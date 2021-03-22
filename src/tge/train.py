import torch
import numpy as np
import pandas as pd
import warnings
import os
from pathlib import Path
from tqdm import tqdm
from torch_geometric.data import Data
from tge.model import HarmonicEncoder, PositionEncoder
from xww.utils.training import get_device, get_optimizer, Recorder
from xww.utils.multiprocessing import MultiProcessor
from xww.utils.profile import profiler


class ConditionalIntensityFunction():
    """ the conditional intensity function \lambda, if given observed history """
    def __init__(self, model):
        self.model = model

    def __call__(self, u, v, T, t):
        """ \lambda^{u, v}(t|T_{u, v}) 
        Args: 
            u, v: node pair
            t: target time for value calculation
            T: observed timestamps before t.
        """
        pass

    def integral(self, u, v, T, t_start, t_end):
        """ integral from t_start to t_end. By MC or by closed-form.
        Args: 
        Args:
            u, v: the node pair
            t_start: integral start
            t_end: integral end
            T: observed timestamps `before` t_start. max(T) <= t_start
        """
        pass

# COMPLETED: f function
# COMPLETED: predict function
class ConditionalDensityFunction():
    """ the conditional density function f(t) """
    def __init__(self, lambdaf): # can not use self.lambda as variable name. `lambda` cannot appear as a variabel
        """ lambdaf is a conditional intensity function instance """
        # self.model = model
        self.lambdaf = lambdaf
    
    def __call__(self, u, v, T, t, **kwargs):
        """ f^{u, v}(t|T_{u, v}) = \lambda^{u, v}(t|T_{u, v}) * \int_{0}^{max(T_{u, v})} \lambda(s) ds
        COMPLETED: to support batch computation of f(t), where t is a batch.
        make sure t.min() >= T.max().

        Args: 
            T: timestamps history, a batch
            t: to support t as a batch
        """
        # assert t.min() >= T.max(), 't should >= T.max(), i.e., t_n'
        lambda_t = self.lambdaf(u, v, T, t, **kwargs)
        integral = self.lambdaf.integral(u, v, T, T[-1], t, **kwargs) # NOTE: integral() batch mode is the KEY.
        integral = torch.clip(integral, -100, 100)
        f_t = lambda_t * torch.exp(-1 * integral)
        return f_t
    
    def predict(self, u, v, T, **kwargs):
        """ next event time expectation by MC
        """
        device = u.device
        intervals = T[1:] - T[:-1]
        max_interval = intervals.mean() + intervals.var()
        # T_interval = 2 * max_interval
        t_end = T[-1] + max_interval

        counter = 0
        test_value = self(u, v, T, t_end, **kwargs)
        while (test_value < 1e-3 or test_value > 0.1) and t_end > T[-1]: # >1e6 for overflow
            if test_value < 1e-3 and t_end > T[-1]+1e-2: # shrink
                t_end = (T[-1] + t_end)/2
            elif test_value > 0.1: # expand
                t_end = t_end + (t_end - T[-1])/2
            counter = counter + 1
            if counter > 20:
                break
            test_value = self(u, v, T, t_end, **kwargs)

        T_interval = torch.abs(t_end - T[-1])
        # T_interval = intervals.mean() + intervals.var() * 3
        size = 15
        t_samples = torch.linspace(T[-1]+T_interval/size , T[-1] + T_interval, size, device=device) # NOTE: in order
        # values = torch.zeros_like(t_samples)
        # for i, t in enumerate(t_samples):
        #     f_t = self(u, v, T, t, **kwargs)
        #     values[i] = f_t.data.squeeze() # used for MC integral of t*f(t) dt
        
        # NOTE: here it supports batch mode of t_samples
        # if t_samples.min() < T[-1]:
        #     import ipdb; ipdb.set_trace()
        values = self(u, v, T, t_samples, **kwargs)
        values = (T_interval/size) * values # it should be probability now.

        values = values / (values.sum() + 1e-6) # normalilze, the result of this step should be similar with that of the former step.
        t_samples = t_samples.reshape(-1, 1)
        assert t_samples.shape == values.shape
        estimated_expectation = (values * t_samples).sum()
        return estimated_expectation

class HarmonicIntensity(ConditionalIntensityFunction):
    def __init__(self, model):
        super(HarmonicIntensity, self).__init__(model)
        pass
        
    def __call__(self, u, v, t, T):
        self.model.eval()
        hid_u, hid_v, emb_u, emb_v = self.model.hidden_rep[u], self.model.hidden_rep[v], self.model.embedding(u), self.model.embedding(v)
        C1 = constant_1(self.model, hid_u, hid_v, emb_u, emb_v)
        lambda_t = torch.exp(C1) + self.model.time_encoder.cos_encoding_mean(t - T, u, v).sum()
        # lambda_t = lambda_t.item()
        return lambda_t
    
    def integral(self, u, v, T, t_start, t_end):
        """ by closed-form """
        # self.model.eval()
        # import ipdb; ipdb.set_trace()
        # lambda_t = self.lambdaf(u, v, t_end, T)
        hid_u, hid_v, emb_u, emb_v = self.model.hidden_rep[u], self.model.hidden_rep[v], self.model.embedding(u), self.model.embedding(v)
        C1 = constant_1(self.model, hid_u, hid_v, emb_u, emb_v)
        integral = (t_end - T[-1]) * torch.exp(C1) + torch.sum(self.model.time_encoder.sin_divide_omega_mean(t_end - T, u, v) - self.model.time_encoder.sin_divide_omega_mean(T[-1] - T, u, v))
        integral = integral + len(T) * self.model.time_encoder.alpha[u][v] * (t_end - T[-1]) # NOTE: new formula
        # integral = integral.item()
        # integral = torch.clamp(integral, -100, 100)
        return integral

class AttenIntensity(ConditionalIntensityFunction):
    def __init__(self, model):
        super(AttenIntensity, self).__init__(model)
    
    def __call__(self, u, v, T, t, **kwargs):
        # COMPLETED: to support t as a batch
        # COMPLETED: add GNN in the lambda^{u, v}(t|H^{u,v}) computation
        assert isinstance(self.model.time_encoder, PositionEncoder), 'here the time encoder should be PositionEncoder'
        time_encoding_dim = self.model.time_encoder_args['dimension']
        emb_t = self.model.time_encoder(t)
        emb_T = self.model.time_encoder(T)
        emb_t = emb_t.view(t.numel(), 1, time_encoding_dim)
        emb_T = emb_T.view(T.numel(), 1, time_encoding_dim)

        emb_t = emb_t.to(dtype=torch.float32, device=t.device) # https://pytorch.org/docs/stable/generated/torch.nn.MultiheadAttention.html
        emb_T = emb_T.to(dtype=torch.float32, device=t.device)
 
        atten_output, atten_output_weight = self.model.AttenModule(emb_t, emb_T, emb_T) # query, key, value.
        atten_output = atten_output.squeeze(1)

        batch = kwargs['batch']
        # uv_agg = self.model(batch, t.mean()) # t.mean() is an appro.
        uv_agg = self.model(batch, t.min()) # t.min() supports t as a batch, and is somewhat reasonable
        uv_agg = uv_agg.reshape((1, -1)).repeat((t.numel(), 1))
        atten_output = torch.cat([atten_output, uv_agg], axis=1) # COMPLETED: to add gnn representation

        # alpha = relu_plus(1e-1, torch.dot(self.model.alpha(u), self.model.alpha(v)) ) # ensure alpha > 0
        alpha = torch.nn.functional.sigmoid( torch.dot(self.model.alpha(u), self.model.alpha(v)) )
        value = soft_plus(alpha, atten_output @ self.model.W_H ) # ensure >= 0
        return value
    
    def integral(self, u, v, T, t_start, t_end, N=10, **kwargs):
        """ approximate the integral by MC 
        Args:
            t_start: integral start, not a batch
            t_end: integral end, -> to support t_end as a batch. NOTE: this integral batch mode is the KEY of batch mode for predict() of f().
        """
        if t_start is None:
            t_start = 0
        t_end = t_end.reshape(-1) # if t = tensor(1.0), to change to ==> tensor([1.0])
        assert t_end.min() >= t_start
        assert t_start >= T.max(), 't_start should >= T.max(), i.e., t_n'
        device = u.device
        points = [torch.linspace(t_start+1e-6, t_end[i], N, device=device) if i==0 else torch.linspace(t_end[i-1], t_end[i], N, device=device) for i in range(len(t_end)) ] # all interpoloted points[t_start, ..., t_end[0],  ]
        points = torch.cat(points).to(device)
        values = self(u, v, T, points, batch=kwargs['batch']) # NOTE: batch mode for interploted points is the KEY.
        
        intervals = torch.cat([t_start.reshape(-1), t_end])
        intervals = (intervals[1:] - intervals[:-1])/N
        intervals = intervals.reshape(-1, 1).repeat(1, N).reshape(-1, 1)
        assert intervals.shape == values.shape

        values = values * intervals # FIXED: shape problem
        values = values.cumsum(dim=0)
        index = torch.arange(1, t_end.numel()+1, device=device) * N - 1
        return_values = values[index]
        return return_values

def soft_plus(phi, x):
    return phi * torch.log(1 + 1e-1 + torch.exp( x / phi )) # it may cause gradient problem

def relu_plus(phi, x):
    return phi + torch.nn.functional.relu(x)

# def relu_mult(phi, x):
#     return 1e-6 + phi * torch.nn.functional.relu(x)


def log_likelihood(u, v, lambdaf, T, **kwargs):
    """
    Args:
        u, v: node pair
        lambdaf: conditional intensity function
        T: observed hsitory
    """
    # ll = 0
    ll1 = 0
    integral = 0
    for i, t in enumerate(T[1:]):
        ll1 = ll1 + torch.log( lambdaf(u, v, T[:i+1], t, batch=kwargs['batch']) ) # here lambdaf will = 0 !
        integral = integral + lambdaf.integral(u, v, T[:i+1], T[i], T[i+1], batch=kwargs['batch'])
        pass
    ll = ll1 - integral # log likelihood
    return ll

def constant_1(model, hid_u, hid_v, emb_u, emb_v):
    return model.W_S_( torch.cat([hid_u, hid_v]).view(1, -1) ) + model.W_E_( torch.cat([emb_u, emb_v]).view(1, -1) )

def model_device(model):
    return next(model.parameters()).device


def criterion(model, batch, **kwargs):
    # COMPLETED: batch now has no uv, t
    u, v = batch.nodepair
    T = batch.T
    # uv, t = batch
    # u, v = uv[0]
    # T = t[0]

    lambdaf = kwargs['lambdaf']
    ll = log_likelihood(u, v, lambdaf, T, batch=batch)
    loss = -1 * ll # negative log likelihood


    if torch.isinf(loss) or torch.isnan(loss):
        import ipdb; ipdb.set_trace()


    return loss

# COMPLETED: no gnn, only self-attention for lambda function
# COMPLETED: how to accelerate?/parallelization? -> train is reasonable, then evaluate? ==> make lambda.integral (so f.__call__) supporting batch mode
# @profiler(Path(os.path.dirname(os.path.abspath(__file__)) ).parent.parent/'log'/'profile')
def optimize_epoch(model, optimizer, train_loader, args, logger, **kwargs):
    model.train() # train mode
    device = get_device(args.gpu)
    model = model.to(device)
    batch_counter = 0
    recorder = []

    for i, batch in tqdm(enumerate(train_loader), total=len(train_loader)):
        if isinstance(batch, list):
            batch = list(map(lambda x: x.to(device), batch)) # set device
        elif isinstance(batch, Data):
            batch = batch.to(device)

        lambdaf = kwargs.get('lambdaf')
        try:
            loss = criterion(model, batch, lambdaf=lambdaf)
            batch_counter += 1
            if batch_counter % 8 == 0: # update model parameters for one step
                loss.backward()
                optimizer.step()
                optimizer.zero_grad()
                # model.reset_hidden_rep() # reset hidden representation matrix
                # model.clip_time_encoder_weight()
                batch_counter = 0
                # torch.cuda.empty_cache()
            else:
                # loss.backward(retain_graph=True) # NOTE: only necessary when cached hidden rep of gnn model is used
                loss.backward() 
            recorder.append(loss.item())
        except Exception as e:
            if 'CUDA out of memory' in e.args[0]:
                # logger.info(f'CUDA out of memory for batch {i}, skipped.')
                pass
            else: 
                raise
        
        if args.debug and i == 10:
            break

    # model.prepare_hidden_rep() # update hidden_rep, for next optimize epoch or for evaluation
    return np.mean(recorder)


def batch_evaluate(model, batch, **kwargs):
    device = model.W_S_.weight.device
    if isinstance(batch, list):
        batch = list(map(lambda x: x.to(device), batch)) # set device
    elif isinstance(batch, Data):
        batch = batch.to(device)

    u, v = batch.nodepair
    T_all = batch.T
    T, t = T_all[:-1], T_all[-1]
   
    lambdaf = kwargs.get('lambdaf')
    ff = ConditionalDensityFunction(lambdaf)

    # evaluate metrics
    loss = criterion(model, batch, lambdaf=lambdaf) # tensor
    pred = ff.predict(u, v, T, batch=batch)
    se = (pred - t)**2 # item
    se = se.cpu().item()
    abs = np.abs((pred - t).cpu().item())
    abs_ratio = abs / ((T_all[1:] - T_all[:-1]).mean().cpu().item() + 1e-6) # now the denominator is average interval
    
    # COMPLETED: record T_all[1:]-T_all[:-1], pred - T_all[-1]. record ff( `some points` )
    if kwargs.get('debug', None):
        intervals = T[1:] - T[:-1]
        # max_interval = intervals.mean() + intervals.var()
        # t_end = T[-1] + max_interval
        # counter = 0
        # test_value = ff(u, v, T, t_end, batch=batch)
        # while (test_value < 1e-3 or test_value > 0.1) and t_end > T[-1]: # >1e6 for overflow
        #     if test_value < 1e-3 and t_end > T[-1]+1e-2: # shrink
        #         t_end = (T[-1] + t_end)/2
        #     elif test_value > 0.1: # expand
        #         t_end = t_end + (t_end - T[-1])/2
        #     counter = counter + 1
        #     if counter > 20:
        #         break
        #     test_value = ff(u, v, T, t_end, batch=batch)
        # T_interval = torch.abs(t_end - T[-1])
        T_interval = intervals.mean() + intervals.var()

        N = 100
        points = torch.linspace(T[-1]+T_interval/N, T[-1]+T_interval, N, device=device)
        f_values = ff(u, v, T, points, batch=batch)
        f_values = f_values * T_interval / N
        f_values = f_values.cpu().numpy().flatten()

        intervals = (T_all[1:] - T_all[:-1]).cpu().numpy().flatten()
        intervals = np.concatenate([intervals, pred.reshape(-1).cpu().numpy()] )
        # need to record values, intervals
        return {'loss': loss.item(), 'se': se, 'abs_ratio': abs_ratio, 'f_values': f_values, 'intervals': intervals}
    else:
        return {'loss': loss.item(), 'se': se, 'abs_ratio': abs_ratio}
        
# @profiler(Path(os.path.dirname(os.path.abspath(__file__)) ).parent.parent/'log'/'profile')
def evaluate(model, test_loader, args, logger, **kwargs):
    model.eval() # eval mode
    device = get_device(args.gpu)
    model = model.to(device)

    loss_recorder = []
    se_recorder = [] # square error
    abs_ratio_recorder = [] # abs(pred-target) / target
    
    if kwargs.get('parallel', None) is not None:
        # batch_evaluater = batch_evaluate_wraper(model, time_encoder, batch_evaluate)
        mp = MultiProcessor(40)
        # result = mp.run_imap(batch_evaluater, test_loader)
        result = mp.run_queue(batch_evaluate, test_loader, model=model, **kwargs)
        result = pd.DataFrame(result)
        loss_recorder = result['loss'].tolist()
        se_recorder = result['se'].tolist()
        abs_ratio_recorder = result['abs_ratio'].tolist()
    else:
        for i, batch_test in tqdm(enumerate(test_loader), total=len(test_loader)):
            with torch.no_grad():
                batch_result = batch_evaluate(model, batch_test, lambdaf=kwargs['lambdaf'], debug=args.debug)
            loss_recorder.append(batch_result['loss'])
            se_recorder.append(batch_result['se'])
            abs_ratio_recorder.append(batch_result['abs_ratio'])
            
            if args.debug and kwargs.get('recorder', None) is not None:
                recorder = kwargs['recorder']
                recorder['f_values'].append(batch_result['f_values'])
                recorder['intervals'].append(batch_result['intervals'])

            if args.debug and i == 100:
                break
    
    return {'loss': np.mean(loss_recorder), 'rmse': np.sqrt(np.mean(se_recorder)), 'abs_ratio': np.mean(abs_ratio_recorder)}
        
# COMPLETED: add recorder to save model state, then check the predic function.
def train_model(model, dataloaders, args, logger):
    train_loader, val_loader, test_loader = dataloaders
    # time_encoder = HarmonicEncoder(args.time_encoder_dimension)
    optimizer = get_optimizer(model, args.optimizer, args.lr, args.l2)
    lambdaf = AttenIntensity(model)
    recorder = Recorder({'loss': 0}, args.checkpoint_dir, args.dataset, args.time_str)
    # results = evaluate(model, train_loader, test_loader, args, logger) # evaluate with random model
    # logger.info(f"Without training, test_loss: {results['loss']:.4f}, test_rmse: {results['rmse']:.4f}, test_abs_ratio: {results['abs_ratio']:.4f}")
    for i in range(args.epochs):
        train_loss = optimize_epoch(model, optimizer, train_loader, args, logger, epoch=i, lambdaf=lambdaf)
        recorder.save_model(model, i=i)
        results = evaluate(model, test_loader, args, logger, epoch=i, lambdaf=lambdaf)
        logger.info(f"Epoch {i}, train_loss: {train_loss:.4f}, test_loss: {results['loss']:.4f}, test_rmse: {results['rmse']:.4f}, test_abs_ratio: {results['abs_ratio']:.6f}")
        recorder.append_full_metrics({'loss': train_loss}, 'train')
        recorder.append_full_metrics({'loss': results['loss'], 'rmse': results['rmse'], 'abs_ratio': results['abs_ratio']}, 'test')
        recorder.save_record()
    logger.info(f"Training finished, best test loss: , best test rmse: , btest abs_ratio: ")

# COMPLETED: debug evaluate_state_dict()
# COMPLETED: optimize/check loss and predict
def evaluate_state_dict(model, dataloaders, args, logger, **kwargs):
    """ evaluate using an existing time_str checkpoint """
    time_str = args.eval
    state_dict_filename = args.state_dict
    device = get_device(args.gpu)
    recorder = Recorder(minmax={}, checkpoint_dir=args.checkpoint_dir, dataset=args.dataset, time_str=time_str)
    # model_states = recorder.load_model(time_str, state_dict_filename)
    # state_dict = model_states.get(state_dict_filename, None)
    state_dict = recorder.load_model(time_str, state_dict_filename)
    assert state_dict is not None, f"No {state_dict_filename}.state_dict in {args.dataset}/{time_str}/ dir"
    # import ipdb; ipdb.set_trace()
    model.load_state_dict(state_dict)
    model = model.to(device)
    lambdaf = AttenIntensity(model)
    logger.info(f'Evaluate using {time_str} state_dict.')
    train_loader, val_loader, test_loader = dataloaders
    results = evaluate(model, test_loader, args, logger, lambdaf=lambdaf, recorder=recorder)
    # import ipdb; ipdb.set_trace()
    recorder.save_record()
    logger.info(f"Eval, test_loss: {results['loss']:.4f}, test_rmse: {results['rmse']:.4f}, test_abs_ratio: {results['abs_ratio']:.4f}")









