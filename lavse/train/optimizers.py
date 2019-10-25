import torch
import torch.nn as nn

from ..utils.logger import get_logger

logger = get_logger()

# Inspired from https://github.com/jnhwkim/ban-vqa/blob/master/train.py
class BanOptimizer():

    def __init__(self,
            parameters,
            name='Adamax',
            lr=0.0007,
            gradual_warmup_steps=[0.5, 2.0, 4],
            lr_decay_epochs=[10, 20, 2],
            lr_decay_rate=.25):

        logger.info(f'lr {lr}')
        logger.info(f'{gradual_warmup_steps}')
        logger.info(f'{lr_decay_epochs}')

        self.optimizer = torch.optim.__dict__[name](
            filter(lambda p: p.requires_grad, parameters),
            lr=lr
        )
        self.lr_decay_rate = lr_decay_rate
        self.lr_decay_epochs = eval("range({},{},{})".format(*lr_decay_epochs))

        self.gradual_warmup_steps = [
            weight * lr for weight in eval("torch.linspace({},{},{})".format(
                gradual_warmup_steps[0],
                gradual_warmup_steps[1],
                int(gradual_warmup_steps[2])
            ))
        ]
        self.grad_clip = .25
        self.total_norm = 0
        self.count_norm = 0
        # if engine:
        #     engine.register_hook('train_on_start_epoch', self.set_lr)
        #     engine.register_hook('train_on_print', self.display_norm)
        self.iteration = 0

    def set_lr(self):
        epoch_id = self.iteration
        for param_group in self.optimizer.param_groups:
            new_lr = self.update_lr(param_group, epoch_id)
            if 'name' in param_group and 'lr_mult' in param_group:
                param_group['lr'] = new_lr * param_group['lr_mult']
            # logger.info('Decrease lr: {:.8f} -> {:.8f}'.format(old_lr, new_lr))

        # logger.info('No change to lr: {:.8f}'.format(old_lr))
        # logger.info('train_epoch.lr {}'.format(optim.param_groups[0]['lr'].item()))

    def update_lr(self, param_group, epoch_id):
        old_lr = param_group['lr']
        if epoch_id < len(self.gradual_warmup_steps):
            new_lr = self.gradual_warmup_steps[epoch_id]
            param_group['lr'] = new_lr
            # logger.info('Gradual Warmup lr: {:.8f} -> {:.8f}'.format(old_lr, new_lr))
        elif epoch_id in self.lr_decay_epochs:
            new_lr = param_group['lr'] * self.lr_decay_rate
            param_group['lr'] = new_lr
        else:
            new_lr = old_lr
        return new_lr

    def display_norm(self):
        logger.info('      norm: {:.5f}'.format(self.total_norm / self.count_norm))

    def step(self):
        # self.total_norm += nn.utils.clip_grad_norm_(
        #     self.engine.model.network.parameters(),
        #     self.grad_clip
        # )
        self.iteration += 1
        self.count_norm += 1
        self.optimizer.step()
        self.set_lr()
        # logger.info('train_batch.norm', self.total_norm / self.count_norm)

    def zero_grad(self):
        self.optimizer.zero_grad()

    def state_dict(self):
        state = {}
        state['optimizer'] = self.optimizer.state_dict()
        return state

    def load_state_dict(self, state):
        self.optimizer.load_state_dict(state['optimizer'])

    def __getattr__(self, key):
        return self.optimizer.__getattribute__(key)


_optimizers = {
    'adam': torch.optim.Adam,
    'adamax': BanOptimizer,
}

def get_optimizer(name, parameters, **kwargs):
    return _optimizers[name](parameters, **kwargs)
