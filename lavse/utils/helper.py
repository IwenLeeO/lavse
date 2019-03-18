import os
import torch
from tensorboardX import SummaryWriter


def save_checkpoint(
        outpath, model, optimizer,
        epoch, args, is_best, classes,
    ):

    if optimizer is not None:
        optimizer = optimizer.state_dict()

    state_dict = {
        'model': model.state_dict(),
        'optimizer': optimizer,
        'args': args,
        'epoch': epoch,
        'classes': classes,
    }

    torch.save(
        obj=state_dict,
        f=os.path.join(outpath, 'checkpoint.pkl'),
    )

    if is_best:
        import shutil
        shutil.copy(
            os.path.join(outpath, 'checkpoint.pkl'),
            os.path.join(outpath, 'best_model.pkl'),
        )


def restore_checkpoint(path, model=None, optimizer=None):
    state_dict = torch.load(path,  map_location=lambda storage, loc: storage)
        
    model.load_state_dict(state_dict['model'])
    if optimizer is not None:
        optimizer.load_state_dict(state_dict['optimizer'])

    return {
        'model': model,
        'optimizer': optimizer,
        'args': state_dict['args'],
        'epoch': state_dict['epoch'],
        'classes': state_dict['classes'],
    }


def adjust_learning_rate(
        optimizer, epoch, initial_lr=1e-3,
        interval=1, decay=0.
    ):

    lr = initial_lr * (decay ** (epoch // interval))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def get_tb_writer(logger_path):

    if logger_path == 'runs/':
        tb_writer = SummaryWriter()
        logger_path = tb_writer.file_writer.get_logdir()
    else:
        tb_writer = SummaryWriter(logger_path)

    return tb_writer


def get_device(gpu_id):

    if gpu_id >= 0:
        device = torch.device('cuda:{}'.format(gpu_id))
    else:
        device = torch.device('cpu')

    return device
