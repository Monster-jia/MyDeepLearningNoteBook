import sys
import os
import argparse
import random
import time
import warnings
import math
BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(BASE_DIR)
warnings.filterwarnings('ignore')

import apex
from apex import amp
from apex.parallel import convert_syncbn_model
from apex.parallel import DistributedDataParallel
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import torch.optim
from torch.utils.data import DataLoader
from config import Config
from public.imagenet import models
from public.imagenet.utils import DataPrefetcher, get_logger, AverageMeter, accuracy
from torch.nn import functional as F

def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    parser.add_argument('--network',
                        type=str,
                        default=Config.network,
                        help='name of network')
    parser.add_argument('--lr',
                        type=float,
                        default=Config.lr,
                        help='learning rate')
    parser.add_argument('--momentum',
                        type=float,
                        default=Config.momentum,
                        help='momentum')
    parser.add_argument('--weight_decay',
                        type=float,
                        default=Config.weight_decay,
                        help='weight decay')
    parser.add_argument('--epochs',
                        type=int,
                        default=Config.epochs,
                        help='num of training epochs')
    parser.add_argument('--per_node_batch_size',
                        type=int,
                        default=Config.per_node_batch_size,
                        help='per_node batch size')
    parser.add_argument('--milestones',
                        type=list,
                        default=Config.milestones,
                        help='optimizer milestones')
    parser.add_argument('--accumulation_steps',
                        type=int,
                        default=Config.accumulation_steps,
                        help='gradient accumulation steps')
    parser.add_argument('--pretrained',
                        type=bool,
                        default=Config.pretrained,
                        help='load pretrained model params or not')
    parser.add_argument('--num_classes',
                        type=int,
                        default=Config.num_classes,
                        help='model classification num')
    parser.add_argument('--input_image_size',
                        type=int,
                        default=Config.input_image_size,
                        help='input image size')
    parser.add_argument('--num_workers',
                        type=int,
                        default=Config.num_workers,
                        help='number of worker to load data')
    parser.add_argument('--resume',
                        type=str,
                        default=Config.resume,
                        help='put the path to resuming file if needed')
    parser.add_argument('--checkpoints',
                        type=str,
                        default=Config.checkpoint_path,
                        help='path for saving trained models')
    parser.add_argument('--log',
                        type=str,
                        default=Config.log,
                        help='path to save log')
    parser.add_argument('--evaluate',
                        type=str,
                        default=Config.evaluate,
                        help='path for evaluate model')
    parser.add_argument('--seed', type=int, default=Config.seed, help='seed')
    parser.add_argument('--print_interval',
                        type=bool,
                        default=Config.print_interval,
                        help='print interval')
    parser.add_argument('--apex',
                        type=bool,
                        default=Config.apex,
                        help='use apex or not')
    parser.add_argument('--sync_bn',
                        type=bool,
                        default=Config.sync_bn,
                        help='use sync bn or not')
    parser.add_argument('--local_rank',
                        type=int,
                        default=0,
                        help='LOCAL_PROCESS_RANK')

    return parser.parse_args()

def main():
    args = parse_args()
    global local_rank
    global rank_value
    local_rank = args.local_rank
    rank_value = args.local_rank
    if local_rank == rank_value:
        global logger
        logger = get_logger(__name__, args.log)

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        cudnn.deterministic = True

    #torch.cuda.set_device(4)
    os.environ["CUDA_VISIBLE_DEVICES"]="4,5,6,7"
    global gpus_num
    gpus_num = torch.cuda.device_count()
    print('begin',gpus_num)
    dist.init_process_group(backend='nccl')
    
    if local_rank == 0:
        logger.info(f'use {gpus_num} gpus')
        logger.info(f"args: {args}")

    cudnn.benchmark = True
    cudnn.enabled = True
    start_time = time.time()

    if local_rank == rank_value:
        logger.info('start loading data')

    train_sampler = torch.utils.data.distributed.DistributedSampler(
        Config.train_dataset, shuffle=True)
    train_loader = DataLoader(Config.train_dataset,
                              batch_size=args.per_node_batch_size,
                              shuffle=False,
                              pin_memory=True,
                              num_workers=args.num_workers,
                              sampler=train_sampler)
    val_loader = DataLoader(Config.val_dataset,
                            batch_size=args.per_node_batch_size,
                            shuffle=False,
                            pin_memory=True,
                            num_workers=args.num_workers)
    if local_rank == rank_value:
        logger.info('finish loading data')

    if local_rank == rank_value:
        logger.info(f"creating model '{args.network}'")
    model = models.__dict__[args.network](**{
        "pretrained": args.pretrained,
        "num_classes": args.num_classes,
    })

    if local_rank == rank_value:
        logger.info(
            f"model: '{args.network}', flops: {flops}, params: {params}")

    for name, param in model.named_parameters():
        if local_rank == rank_value:
            logger.info(f"{name},{param.requires_grad}")

    model = model.cuda()
    criterion = nn.CrossEntropyLoss().cuda()
    optimizer = torch.optim.SGD(model.parameters(),
                                args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=args.milestones, gamma=0.1)

    if args.sync_bn:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    if args.apex:
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1')
        model = apex.parallel.DistributedDataParallel(model,
                                                      delay_allreduce=True)
        if args.sync_bn:
            model = apex.parallel.convert_syncbn_model(model)
    else:
        print('ok')
        model = nn.parallel.DistributedDataParallel(model)
        print('fucked')
    if args.evaluate:
        # load best model
        if not os.path.isfile(args.evaluate):
            if local_rank == rank_value:
                logger.exception(
                    '{} is not a file, please check it again'.format(
                        args.resume))
            sys.exit(-1)
        if local_rank == rank_value:
            logger.info('start only evaluating')
            logger.info(f"start resuming model from {args.evaluate}")
        checkpoint = torch.load(args.evaluate,
                                map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint['model_state_dict'])
        acc1, acc5, throughput, rb_loss = validate(val_loader, model, args)
        if local_rank == rank_value:
            logger.info(
                f"epoch {checkpoint['epoch']:0>3d}, top1 acc: {acc1:.2f}%, top5 acc: {acc5:.2f}%, throughput: {throughput:.2f}sample/s"
            )

        return

    start_epoch = 1
    min_rb_loss = 1000
    # resume training
    if os.path.exists(args.resume):
        if local_rank == rank_value:
            logger.info(f"start resuming model from {args.resume}")
        checkpoint = torch.load(args.resume, map_location=torch.device('cpu'))
        start_epoch += checkpoint['epoch']
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        if local_rank == rank_value:
            logger.info(
                f"finish resuming model from {args.resume}, epoch {checkpoint['epoch']}, "
                f"loss: {checkpoint['loss']:3f}, lr: {checkpoint['lr']:.6f}, "
                f"top1_acc: {checkpoint['acc1']}%")

    if not os.path.exists(args.checkpoints):
        os.makedirs(args.checkpoints)

    if local_rank == rank_value:
        logger.info('start training')
    for epoch in range(start_epoch, args.epochs + 1):
        train_sampler.set_epoch(epoch)
        acc1, acc5, losses = train(train_loader, model, criterion, optimizer,
                                   scheduler, epoch, args)
        if local_rank == rank_value:
            logger.info(
                f"train: epoch {epoch:0>3d}, top1 acc: {acc1:.2f}%, top5 acc: {acc5:.2f}%, losses: {losses:.2f}"
            )

        acc1, acc5, throughput, rb_loss = validate(val_loader, model, args)
        if local_rank == rank_value:
            logger.info(
                f"val: epoch {epoch:0>3d}, top1 acc: {acc1:.2f}%, top5 acc: {acc5:.2f}%, throughput: {throughput:.2f}sample/s"
            )

        # remember best prec@1 and save checkpoint
        if local_rank == rank_value:
            if rb_loss < min_rb_loss:
                min_rb_loss = rb_loss
                logger.info("save model")
                torch.save(
                    {
                        'epoch': epoch,
                        'acc1': acc1,
                        'loss': losses,
                        'lr': scheduler.get_lr()[0],
                        'model_state_dict': model.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict(),
                        'scheduler_state_dict': scheduler.state_dict(),
                    }, os.path.join(args.checkpoints, 'latest.pth'))
            if epoch == args.epochs:
                if local_rank == rank_value:
                    torch.save(
                    {
                        'epoch': epoch,
                        'acc1': acc1,
                        'loss': losses,
                        'lr': scheduler.get_lr()[0],
                        'model_state_dict': model.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict(),
                        'scheduler_state_dict': scheduler.state_dict(),
                    },
                    os.path.join(
                        args.checkpoints,
                        "{}-epoch{}-acc{}.pth".format(args.network, epoch, acc1)))

    training_time = (time.time() - start_time) / 3600
    if local_rank == rank_value:
        logger.info(
            f"finish training, total training time: {training_time:.2f} hours")


def train(train_loader, model, criterion, optimizer, scheduler, epoch, args):
    top1 = AverageMeter()
    top5 = AverageMeter()
    losses = AverageMeter()

    # switch to train mode
    model.train()

    iters = len(train_loader.dataset) // (args.per_node_batch_size * gpus_num)
    prefetcher = DataPrefetcher(train_loader)
    inputs, labels = prefetcher.next()
    iter_index = 1
    while inputs is not None:
        inputs, labels = inputs.cuda(), labels.cuda()

        outputs = model(inputs)
        loss = criterion(outputs, labels)
        loss = loss / args.accumulation_steps

        if args.apex:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if iter_index % args.accumulation_steps == 0:
            optimizer.step()
            optimizer.zero_grad()

        # measure accuracy and record loss
        acc1, acc5 = accuracy(outputs, labels, topk=(1, 2))
        top1.update(acc1.item(), inputs.size(0))
        top5.update(acc5.item(), inputs.size(0))
        losses.update(loss.item(), inputs.size(0))

        inputs, labels = prefetcher.next()

        if local_rank == rank_value and iter_index % args.print_interval == 0:
            logger.info(
                f"train: epoch {epoch:0>3d}, iter [{iter_index:0>4d}, {iters:0>4d}], lr: {scheduler.get_lr()[0]:.6f}, top1 acc: {acc1.item():.2f}%, top5 acc: {acc5.item():.2f}%, loss_total: {loss.item():.2f}"
            )

        iter_index += 1

    scheduler.step()

    return top1.avg, top5.avg, losses.avg


def validate(val_loader, model, args):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()
    result = 0
    with torch.no_grad():
        end = time.time()
        for inputs, labels in val_loader:
            data_time.update(time.time() - end)
            inputs, labels = inputs.cuda(), labels.cuda()
            outputs = model(inputs)
            outputs = F.softmax(outputs, dim=1)
            acc1, acc5 = accuracy(outputs, labels, topk=(1, 2))
            top1.update(acc1.item(), inputs.size(0))
            top5.update(acc5.item(), inputs.size(0))
            batch_time.update(time.time() - end)
            end = time.time()
            # compute softmax loss
            pred_score = outputs.cuda().data.cpu().numpy()
            for i in range(len(labels)):
                for j in range(4):
                    if labels[i] == j and pred_score[i][j]>0:
                        result += math.log(pred_score[i][j])
            labels_len += len(labels)
        result = -result/labels_len
        logger.info(f'rb loss: {result}')

    throughput = 1.0 / (batch_time.avg / inputs.size(0))

    return top1.avg, top5.avg, throughput, result


if __name__ == '__main__':
    main()