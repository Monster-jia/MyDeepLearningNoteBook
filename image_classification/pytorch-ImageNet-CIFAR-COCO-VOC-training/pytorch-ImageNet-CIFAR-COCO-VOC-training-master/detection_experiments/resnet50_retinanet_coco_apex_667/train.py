import sys
import os
import argparse
import random
import shutil
import time
import warnings
import json

BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(BASE_DIR)
warnings.filterwarnings('ignore')

import numpy as np
from thop import profile
from thop import clever_format
from apex import amp
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torchvision import transforms
from config import Config
from public.detection.dataset.cocodataset import COCODataPrefetcher, collater
from public.detection.models.loss import RetinaLoss
from public.detection.models.decode import RetinaDecoder
from public.detection.models.retinanet import resnet50_retinanet
from public.imagenet.utils import get_logger
from pycocotools.cocoeval import COCOeval


def parse_args():
    parser = argparse.ArgumentParser(
        description='PyTorch COCO Detection Training')
    parser.add_argument('--network',
                        type=str,
                        default=Config.network,
                        help='name of network')
    parser.add_argument('--lr',
                        type=float,
                        default=Config.lr,
                        help='learning rate')
    parser.add_argument('--epochs',
                        type=int,
                        default=Config.epochs,
                        help='num of training epochs')
    parser.add_argument('--batch_size',
                        type=int,
                        default=Config.batch_size,
                        help='batch size')
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

    return parser.parse_args()


def validate(val_dataset, model, decoder):
    model = model.module
    # switch to evaluate mode
    model.eval()
    with torch.no_grad():
        all_eval_result = evaluate_coco(val_dataset, model, decoder)

    return all_eval_result


def evaluate_coco(val_dataset, model, decoder):
    results, image_ids = [], []
    for index in range(len(val_dataset)):
        data = val_dataset[index]
        scale = data['scale']
        cls_heads, reg_heads, batch_anchors = model(data['img'].cuda().permute(
            2, 0, 1).float().unsqueeze(dim=0))
        scores, classes, boxes = decoder(cls_heads, reg_heads, batch_anchors)
        scores, classes, boxes = scores.cpu(), classes.cpu(), boxes.cpu()
        boxes /= scale

        # make sure decode batch_size=1
        # scores shape:[1,max_detection_num]
        # classes shape:[1,max_detection_num]
        # bboxes shape[1,max_detection_num,4]
        assert scores.shape[0] == 1

        scores = scores.squeeze(0)
        classes = classes.squeeze(0)
        boxes = boxes.squeeze(0)

        # for coco_eval,we need [x_min,y_min,w,h] format pred boxes
        boxes[:, 2:] -= boxes[:, :2]

        for object_score, object_class, object_box in zip(
                scores, classes, boxes):
            object_score = float(object_score)
            object_class = int(object_class)
            object_box = object_box.tolist()
            if object_class == -1:
                break

            image_result = {
                'image_id':
                val_dataset.image_ids[index],
                'category_id':
                val_dataset.find_category_id_from_coco_label(object_class),
                'score':
                object_score,
                'bbox':
                object_box,
            }
            results.append(image_result)

        image_ids.append(val_dataset.image_ids[index])

        print('{}/{}'.format(index, len(val_dataset)), end='\r')

    if not len(results):
        print("No target detected in test set images")
        return

    json.dump(results,
              open('{}_bbox_results.json'.format(val_dataset.set_name), 'w'),
              indent=4)

    # load results in COCO evaluation tool
    coco_true = val_dataset.coco
    coco_pred = coco_true.loadRes('{}_bbox_results.json'.format(
        val_dataset.set_name))

    coco_eval = COCOeval(coco_true, coco_pred, 'bbox')
    coco_eval.params.imgIds = image_ids
    coco_eval.evaluate()
    coco_eval.accumulate()
    coco_eval.summarize()
    all_eval_result = coco_eval.stats

    return all_eval_result


def train(train_loader, model, criterion, optimizer, scheduler, epoch, logger,
          args):
    cls_losses, reg_losses, losses = [], [], []

    # switch to train mode
    model.train()

    iters = len(train_loader.dataset) // args.batch_size
    prefetcher = COCODataPrefetcher(train_loader)
    images, annotations = prefetcher.next()
    iter_index = 1

    while images is not None:
        images, annotations = images.cuda().float(), annotations.cuda()
        cls_heads, reg_heads, batch_anchors = model(images)
        cls_loss, reg_loss = criterion(cls_heads, reg_heads, batch_anchors,
                                       annotations)
        loss = cls_loss + reg_loss
        if cls_loss == 0.0 or reg_loss == 0.0:
            optimizer.zero_grad()
            continue

        if args.apex:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), 0.1)
        optimizer.step()
        optimizer.zero_grad()

        cls_losses.append(cls_loss.item())
        reg_losses.append(reg_loss.item())
        losses.append(loss.item())

        images, annotations = prefetcher.next()

        if iter_index % args.print_interval == 0:
            logger.info(
                f"train: epoch {epoch:0>3d}, iter [{iter_index:0>5d}, {iters:0>5d}], cls_loss: {cls_loss.item():.2f}, reg_loss: {reg_loss.item():.2f}, loss_total: {loss.item():.2f}"
            )

        iter_index += 1

    scheduler.step(np.mean(losses))

    return np.mean(cls_losses), np.mean(reg_losses), np.mean(losses)


def main(logger, args):
    if not torch.cuda.is_available():
        raise Exception("need gpu to train network!")

    torch.cuda.empty_cache()

    if args.seed is not None:
        random.seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        cudnn.deterministic = True

    gpus = torch.cuda.device_count()
    logger.info(f'use {gpus} gpus')
    logger.info(f"args: {args}")

    cudnn.benchmark = True
    cudnn.enabled = True
    start_time = time.time()

    # dataset and dataloader
    logger.info('start loading data')
    train_loader = DataLoader(Config.train_dataset,
                              batch_size=args.batch_size,
                              shuffle=True,
                              num_workers=args.num_workers,
                              collate_fn=collater)
    logger.info('finish loading data')

    model = resnet50_retinanet(**{
        "pretrained": args.pretrained,
        "num_classes": args.num_classes,
    })

    for name, param in model.named_parameters():
        logger.info(f"{name},{param.requires_grad}")

    flops_input = torch.randn(1, 3, args.input_image_size,
                              args.input_image_size)
    flops, params = profile(model, inputs=(flops_input, ))
    flops, params = clever_format([flops, params], "%.3f")
    logger.info(f"model: '{args.network}', flops: {flops}, params: {params}")

    criterion = RetinaLoss(image_w=args.input_image_size,
                           image_h=args.input_image_size).cuda()
    decoder = RetinaDecoder(image_w=args.input_image_size,
                            image_h=args.input_image_size).cuda()

    model = model.cuda()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,
                                                           patience=3,
                                                           verbose=True)

    if args.apex:
        amp.register_float_function(torch, 'sigmoid')
        amp.register_float_function(torch, 'softmax')
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1')

    model = nn.DataParallel(model)

    if args.evaluate:
        if not os.path.isfile(args.evaluate):
            raise Exception(
                f"{args.resume} is not a file, please check it again")
        logger.info('start only evaluating')
        logger.info(f"start resuming model from {args.evaluate}")
        checkpoint = torch.load(args.evaluate,
                                map_location=torch.device('cpu'))
        model.load_state_dict(checkpoint['model_state_dict'])
        all_eval_result = validate(Config.val_dataset, model, decoder)
        if all_eval_result is not None:
            logger.info(
                f"val: epoch: {checkpoint['epoch']:0>5d}, IoU=0.5:0.95,area=all,maxDets=100,mAP:{all_eval_result[0]:.3f}, IoU=0.5,area=all,maxDets=100,mAP:{all_eval_result[1]:.3f}, IoU=0.75,area=all,maxDets=100,mAP:{all_eval_result[2]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAP:{all_eval_result[3]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAP:{all_eval_result[4]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAP:{all_eval_result[5]:.3f}, IoU=0.5:0.95,area=all,maxDets=1,mAR:{all_eval_result[6]:.3f}, IoU=0.5:0.95,area=all,maxDets=10,mAR:{all_eval_result[7]:.3f}, IoU=0.5:0.95,area=all,maxDets=100,mAR:{all_eval_result[8]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAR:{all_eval_result[9]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAR:{all_eval_result[10]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAR:{all_eval_result[11]:.3f}"
            )

        return

    best_map = 0.0
    start_epoch = 1
    # resume training
    if os.path.exists(args.resume):
        logger.info(f"start resuming model from {args.resume}")
        checkpoint = torch.load(args.resume, map_location=torch.device('cpu'))
        start_epoch += checkpoint['epoch']
        best_map = checkpoint['best_map']
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        logger.info(
            f"finish resuming model from {args.resume}, epoch {checkpoint['epoch']}, best_map: {checkpoint['best_map']}, "
            f"loss: {checkpoint['loss']:3f}, cls_loss: {checkpoint['cls_loss']:2f}, reg_loss: {checkpoint['reg_loss']:2f}"
        )

    if not os.path.exists(args.checkpoints):
        os.makedirs(args.checkpoints)

    logger.info('start training')
    for epoch in range(start_epoch, args.epochs + 1):
        cls_losses, reg_losses, losses = train(train_loader, model, criterion,
                                               optimizer, scheduler, epoch,
                                               logger, args)
        logger.info(
            f"train: epoch {epoch:0>3d}, cls_loss: {cls_losses:.2f}, reg_loss: {reg_losses:.2f}, loss: {losses:.2f}"
        )

        if epoch % 5 == 0 or epoch == args.epochs:
            logger.info(f"start eval.")
            all_eval_result = validate(Config.val_dataset, model, decoder)
            logger.info(f"eval done.")
            if all_eval_result is not None:
                logger.info(
                    f"val: epoch: {epoch:0>5d}, IoU=0.5:0.95,area=all,maxDets=100,mAP:{all_eval_result[0]:.3f}, IoU=0.5,area=all,maxDets=100,mAP:{all_eval_result[1]:.3f}, IoU=0.75,area=all,maxDets=100,mAP:{all_eval_result[2]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAP:{all_eval_result[3]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAP:{all_eval_result[4]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAP:{all_eval_result[5]:.3f}, IoU=0.5:0.95,area=all,maxDets=1,mAR:{all_eval_result[6]:.3f}, IoU=0.5:0.95,area=all,maxDets=10,mAR:{all_eval_result[7]:.3f}, IoU=0.5:0.95,area=all,maxDets=100,mAR:{all_eval_result[8]:.3f}, IoU=0.5:0.95,area=small,maxDets=100,mAR:{all_eval_result[9]:.3f}, IoU=0.5:0.95,area=medium,maxDets=100,mAR:{all_eval_result[10]:.3f}, IoU=0.5:0.95,area=large,maxDets=100,mAR:{all_eval_result[11]:.3f}"
                )
                if all_eval_result[0] > best_map:
                    torch.save(model.module.state_dict(),
                               os.path.join(args.checkpoints, "best.pth"))
                    best_map = all_eval_result[0]
        torch.save(
            {
                'epoch': epoch,
                'best_map': best_map,
                'cls_loss': cls_losses,
                'reg_loss': reg_losses,
                'loss': losses,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'scheduler_state_dict': scheduler.state_dict(),
            }, os.path.join(args.checkpoints, 'latest.pth'))

    logger.info(f"finish training, best_map: {best_map:.3f}")
    training_time = (time.time() - start_time) / 3600
    logger.info(
        f"finish training, total training time: {training_time:.2f} hours")


if __name__ == '__main__':
    args = parse_args()
    logger = get_logger(__name__, args.log)
    main(logger, args)
