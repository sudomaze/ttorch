import torch
import torch.nn as nn
import torch.optim as optim
from rich import pretty
pretty.install()
import tqdm
import tqdm.rich
import logging
import random
import os, sys, copy
from omegaconf import OmegaConf
import wandb
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP
from logging.handlers import QueueHandler
from logger import Logger, WorkerLogFilter
class Trainer(object):
    def __init__(self, module, dataset, distributed,**kwargs):
        self.module = module
        self.dataset = dataset
        self.kwargs = copy.deepcopy(kwargs)
        self.trainer_kwargs = self.kwargs['trainer_kwargs']
        self.distributed = distributed
        # if self.distributed: mp.set_start_method("spawn", force=True)
        # self.logger = Logger(
        #     self.kwargs['project'],
        #     self.kwargs['exp_name'],
        #     self.kwargs['save_dir'],
        #     self.kwargs,
        #     distributed
        # )
        self.seed = self.trainer_kwargs['seed'] if 'seed' in self.trainer_kwargs.keys() else random.randint(1, 10000)
        self.history = {'train': {'loss': [], 'correct': []}, 'val': {'loss': [], 'correct': []}}
        self.epoch_history = {'train': {'loss': [], 'acc': []}, 'val': {'loss': [], 'acc': []}}
        # self.events = self.kwargs['events'] if 'events' in self.kwargs.keys() else None

    def init(self,device):
        torch.backends.cudnn.enabled = False
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)
        torch.cuda.set_device(device)

    def reset(self, mode, module):
        if mode == 'train':
            module.model.train()
            torch.set_grad_enabled(True)
            self.history['train']['loss'], self.history['train']['correct'] = [], []
        else:
            module.model.eval()
            torch.set_grad_enabled(False)
            self.history['val']['loss'], self.history['val']['correct'] = [], []

    def store(self, mode, model_output,device):
        output, target, loss = model_output
        if self.distributed:
            loss = self.reduce_dict({'loss': torch.tensor(loss.item()).to(device)})['loss']
        self.history[mode]['loss'].append(loss.item())
        self.history[mode]['correct'].append((output.max(1)[1] == target).sum().item())

    def save(self, module, epoch):
        torch.save(module.model.state_dict(), f'{self.exp_path}/ckpts/{epoch:03d}_model.pth')
        torch.save(module.model.optimizer.state_dict(), f'{self.exp_path}/optims/{epoch:03d}_optimizer.pth')

    def update_history(self, mode):
        total = len(self.history[mode]['loss'])
        if mode == 'train':
            total *= self.kwargs['dataset_kwargs']['batch_size']
        else:
            if 'val_batch_size' in self.kwargs['dataset_kwargs'].keys():
                total *= self.kwargs['dataset_kwargs']['val_batch_size']
            else:
                total *= self.kwargs['dataset_kwargs']['batch_size']
        self.epoch_history[mode]['loss'].append(sum(self.history[mode]['loss']) / total)
        self.epoch_history[mode]['acc'].append(sum(self.history[mode]['correct']) / total)
    
    def update_loop(self, loop, postfix, iter_num=None):
        if iter_num: loop.update(iter_num)
        loop.set_postfix(postfix)
    
    def reset_loop(self, loop, postfix, rich=False):
        loop.refresh()
        loop.reset()
        loop.set_postfix(postfix)
    
    def reduce_dict(self,input_dict, average=True):
        world_size = float(dist.get_world_size())
        names, values = [], []
        for k in sorted(input_dict.keys()):
            names.append(k)
            values.append(input_dict[k])
        values = torch.stack(values, dim=0)
        dist.all_reduce(values, op=dist.ReduceOp.SUM)
        if average:
            values /= world_size
        reduced_dict = {k: v for k, v in zip(names, values)}
        return reduced_dict
    def step(self, mode, module, dataset, step_loop, epoch_loop, epoch, device):
        data_loader = None
        if mode == 'train':
            self.reset(mode, module); step_loop.set_description(f"Epoch {epoch}:  Training") 
            data_loader = dataset.train_loader()
            for batch_idx, batch in tqdm.tqdm(enumerate(data_loader)):
                data, target = batch
                data, target = data.to(device), target.to(device)
                batch = (data, target)
                self.store(
                    mode, 
                    module.training_step(batch),
                    device
                )
                if batch_idx % self.trainer_kwargs['log_interval'] == 0: 
                    self.update_loop(step_loop, {f'{mode}_loss': self.history[mode]['loss'][-1]}, iter_num=self.trainer_kwargs['log_interval'])
        else:
            self.reset('val', module); step_loop.set_description(f"Epoch {epoch}:  Validating")
            data_loader = dataset.val_loader()
            with torch.no_grad():
                for batch_idx, batch in tqdm.tqdm(enumerate(data_loader)):
                    data, target = batch
                    data, target = data.to(device), target.to(device)
                    batch = (data, target)
                    self.store(
                        mode, 
                        module.predicting_step(batch),
                        device
                    )
                    if batch_idx % self.trainer_kwargs['log_interval'] == 0: 
                        self.update_loop(step_loop, {f'{mode}_loss': self.history[mode]['loss'][-1]}, iter_num=self.trainer_kwargs['log_interval'])
        # step
        left_over = len(data_loader) % self.trainer_kwargs['log_interval']
        self.update_loop(
            step_loop, 
            {f'{mode}_loss': self.history[mode]['loss'][-1]}, 
            iter_num=left_over
        )
        self.update_history(mode)
        self.update_loop(
            epoch_loop, 
            {
                f'{mode}_loss': self.epoch_history[mode]['loss'][-1], 
                f'{mode}_acc': self.epoch_history[mode]['acc'][-1]
            }
        )
    def verify(self,module,dataset):
        # TODO: verify train and eval
        step_loop = tqdm.tqdm(
            range(2), 
            bar_format='{l_bar}{bar:20}{r_bar}{bar:-20b}', file=sys.stdout
        )
        with torch.no_grad():
            val_loader = dataset.val_loader()
            for batch_idx, batch in enumerate(val_loader):
                step_loop.update(); step_loop.set_description(f"Validator {batch_idx}")
                self.store('val', module.predicting_step(batch))
                if batch_idx+1 == 2: break
    def start_epoch(self,epoch):
        self.epoch_loop.update(); 
        self.epoch_loop.set_description(f"Epoch {epoch}")
        self.reset_loop(self.step_loop,  {'train_loss': 0, 'val_loss': 0})
        self.step_counter = {'train': 0,'val': 0}
    def before_fit(self,dataset):
        self.epoch_loop = tqdm.tqdm(
            range(1, self.trainer_kwargs['epochs'] + 1), 
            bar_format='{l_bar}{bar:20}{r_bar}{bar:-20b}', file=sys.stdout
        )
        self.step_loop = tqdm.tqdm(
            range(len(dataset.train_loader())+len(dataset.val_loader())), 
            bar_format='{l_bar}{bar:20}{r_bar}{bar:-20b}', file=sys.stdout
        )
        self.reset_loop(self.epoch_loop, {'train_loss': 0, 'train_acc': 0, 'val_loss': 0, 'val_acc': 0})
        self.reset_loop(self.step_loop,  {'train_loss': 0, 'val_loss': 0} )
    def end_epoch(self,module,epoch):
        if ( 
                self.trainer_kwargs['save_interval'] 
                and epoch % self.trainer_kwargs['save_interval'] == 0
                and (
                    not self.distrubted
                    or (
                        self.distrubted
                        and dist.get_rank() == 0
                    )
                )
        ):
            self.save(module, epoch)
        module.scheduler.step()
        print('EEpoch\n',flush=True)
        print(
            f"Epoch {epoch}: \t",
            f"train/val_loss: {self.epoch_history['train']['loss'][-1]:.04f}/{self.epoch_history['val']['loss'][-1]:.04f}\t",
            f"train/val_acc: {self.epoch_history['train']['acc'][-1]:.04f}/{self.epoch_history['val']['acc'][-1]:.04f}\n\n"
        )
        # wandb.log({
        #     f"train_loss": self.epoch_history['train']['loss'][-1],
        #     f"val_loss": self.epoch_history['val']['loss'][-1],
        # },step=epoch)
    def _fit(self):
        module = copy.deepcopy(self.module)
        dataset = copy.deepcopy(self.dataset)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        if self.distributed: 
            device = torch.device(f"cuda:{dist.get_rank()}")
            self.init(dist.get_rank())
        else: self.init(0)
        module.model = module.model.to(device)
        if self.distributed:
            module.model = DDP(module.model,device_ids=[dist.get_rank()],output_device=dist.get_rank())
            dataset.dist_sampler()
        module.init_optimizer()
        # self.verify(module,dataset)
        self.before_fit(dataset)
        for epoch in self.epoch_loop:
            self.start_epoch(epoch)
            self.step('train', module, dataset, self.step_loop, self.epoch_loop, epoch,device)
            self.step('val', module, dataset, self.step_loop, self.epoch_loop, epoch,device)
            self.end_epoch(module,epoch)
    def setup_for_distributed(self,is_master):
        import builtins as __builtin__
        # builtin_print = __builtin__.print

        # def print(*args, **kwargs):
        #     force = kwargs.pop('force', False)
        #     if is_master or force:
        #         builtin_print(*args, **kwargs)
        print('asdasdaasdasdadasasasd', self.logger.info)
        print(self.logger.info)
        __builtin__.print = self.logger.info
    def setup_worker_logging(self,rank):
        queue_handler = QueueHandler(self.logger.log_queue)

        # Add a filter that modifies the message to put the
        # rank in the log format
        worker_filter = WorkerLogFilter(rank)
        queue_handler.addFilter(worker_filter)

        queue_handler.setLevel(logging.INFO)

        root_logger = logging.getLogger()
        root_logger.addHandler(queue_handler)

        # Default logger level is WARNING, hence the change. Otherwise, any worker logs
        # are not going to get bubbled up to the parent's logger handlers from where the
        # actual logs are written to the output
        root_logger.setLevel(logging.INFO)
    def init_process(
        self,
        rank, # rank of the process
        world_size, # number of workers
        # fn, # function to be run
        # backend='gloo',# good for single node
        backend='nccl' # the best for CUDA
    ):
        # rank = dist.get_rank()
        self.setup_worker_logging(rank)
        logging.info("Test worker log")
        logging.error("Test worker error log")
        torch.cuda.set_device(rank)
        # information used for rank 0
        os.environ['MASTER_ADDR'] = self.trainer_kwargs['ip']
        os.environ['MASTER_PORT'] = self.trainer_kwargs['port']
        dist.init_process_group(backend, rank=rank, world_size=world_size)
        # dist.barrier()
        # self.setup_for_distributed(rank == 0)
        # self.setup_for_distributed(False)
        self._fit()
    def fit(self):
        if self.distributed:
            world_size = len(self.trainer_kwargs['gpus'])
            processes = []
            try:
                # print(torch.multiprocessing.get_start_method())
                # mp.set_start_method("spawn")
                ctx = mp.get_context("spawn")
                # mp.spawn(self.init_process, args=(world_size,), nprocs=world_size)
                for rank in range(world_size):
                    print(f"Starting process {rank}")
                    p = ctx.Process(target=self.init_process, args=(rank, world_size))
                    # p = mp.Process(target=self.init_process, args=(world_size,))
                    processes.append(p)
                for p in processes:
                    p.start()
                for p in processes:
                    p.join()
            except RuntimeError as e:
                print(e)
        else:
            self._fit()