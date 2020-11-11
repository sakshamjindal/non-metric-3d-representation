# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/train_model.ipynb (unless otherwise specified).

__all__ = ['setup_tb', 'run_training', 'train', 'val_retrieval']

# Cell
import builtins
import math
import os
import random
import shutil
import time
import warnings
from tqdm import tqdm
import numpy as np
import argparse

# Cell

from tensorboardX import SummaryWriter

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
from torch.utils.data import DataLoader

# Cell

from .model.model import MoCo_scene_and_view as MoCo
from .dataloader import CLEVR_train, collate_boxes, CLEVR_train_onlyquery, collate_boxes_onlyquery, sample_same_scene_negs
from .utils import compute_features, run_kmeans, AverageMeter, ProgressMeter, adjust_learning_rate, accuracy, save_checkpoint, DoublePool_O, store_to_pool, random_retrieve_topk, plot_query_retrieval

# Cell

def setup_tb(exp_name):
    tb_directory = os.path.join('./tb_logs', exp_name)
    return SummaryWriter(tb_directory)

# Cell

def run_training(args):

#     parser = argparse.ArgumentParser(description='Relational 2d Training')

#     if default_args:
#         args = parser.parse_args(default_args)
#     else:
#         args = parser.parse_args()

    tb_logger = setup_tb(args.exp_dir)

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    args.num_cluster = args.num_cluster.split(',')

    if not os.path.exists(os.path.join('./tb_logs',args.exp_dir)):
        os.mkdir(os.path.join('./tb_logs', args.exp_dir))

    ngpus_per_node = torch.cuda.device_count()

    gpu_devices = ','.join([str(id) for id in range(ngpus_per_node)])
    #os.environ["CUDA_VISIBLE_DEVICES"] = gpu_devices

    best_acc = 0

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    print('==> Preparing data..')

    traindir = os.path.join(args.data)
    valdir = os.path.join(args.data[:-5] + 'v.txt')
    moco_train_dataset = CLEVR_train(root_dir=traindir, hyp_N=args.hyp_N)
    moco_train_loader = DataLoader(moco_train_dataset, batch_size=args.batch_size, shuffle=True, collate_fn=collate_boxes)

    kmeans_train_dataset = CLEVR_train_onlyquery(root_dir=traindir, hyp_N=args.hyp_N)
    kmeans_train_loader = DataLoader(kmeans_train_dataset, batch_size=5*args.batch_size, shuffle=False, collate_fn=collate_boxes_onlyquery)

    pool_size = len(moco_train_dataset)

    isnode = False
    if args.mode=='node':
        isnode = True

    pool_e_train = DoublePool_O(pool_size, isnode)
    pool_g_train = DoublePool_O(pool_size, isnode)

    moco_val_dataset = CLEVR_train(root_dir=valdir, hyp_N=args.hyp_N)
    moco_val_loader = DataLoader(moco_val_dataset, batch_size=1, shuffle=True, collate_fn=collate_boxes)

    pool_size = len(moco_val_dataset)
    pool_e_val = DoublePool_O(pool_size, isnode)
    pool_g_val = DoublePool_O(pool_size, isnode)

    print('==> Making model..')

    model = MoCo(mode=args.mode, scene_r=args.scene_r, view_r=args.view_r)
    #model = nn.DataParallel(model)
    model = model.to(device)
    num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('The number of parameters of model is', num_params)


    criterion = nn.CrossEntropyLoss().cuda()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
#             else:
#                 # Map model to be loaded to specified single gpu.
#                 loc = 'cuda:{}'.format(args.gpu)
#                 checkpoint = torch.load(args.resume, map_location=loc)
            args.start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    if args.use_pretrained:
        if os.path.isfile(args.use_pretrained):
            print("=> loading checkpoint '{}'".format(args.use_pretrained))
            state_dict = torch.load(args.use_pretrained)['state_dict']
            del state_dict['queue']
            del state_dict['queue_ptr']

            keys_to_delete = \
            ["mode",
             "encoder_q.spatial_viewpoint_transformation.0.weight",
             "encoder_q.spatial_viewpoint_transformation.0.bias",
             "encoder_q.spatial_viewpoint_transformation.2.weight",
             "encoder_q.spatial_viewpoint_transformation.2.bias",
             "encoder_k.spatial_viewpoint_transformation.0.weight",
             "encoder_k.spatial_viewpoint_transformation.0.bias",
             "encoder_k.spatial_viewpoint_transformation.2.weight",
             "encoder_k.spatial_viewpoint_transformation.2.bias"]

            for k in keys_to_delete:
                    if k in state_dict.keys():
                        del state_dict[k]

            model_dict = model.state_dict()
            model_dict.update(state_dict)

            model.load_state_dict(model_dict)
        else:
            print("=> no checkpoint found at '{}'".format(args.use_pretrained))

    for epoch in range(args.start_epoch, args.epochs):

        cluster_result = None

        if epoch>=args.warmup_epoch:
            # compute momentum features for center-cropped images
            features = compute_features(kmeans_train_loader, model, args)

            # placeholder for clustering result
            cluster_result = {'im2cluster':[],'centroids':[],'density':[]}
            for num_cluster in args.num_cluster:
                cluster_result['im2cluster'].append(torch.zeros(len(kmeans_train_dataset),dtype=torch.long).cuda())
                cluster_result['centroids'].append(torch.zeros(int(num_cluster),256).cuda())
                cluster_result['density'].append(torch.zeros(int(num_cluster)).cuda())

#             features[torch.norm(features,dim=1)>1.5] /= 2 #account for the few samples that are computed twice
            features = features.numpy()
            cluster_result = run_kmeans(features,args)  #run kmeans clustering on master node
                # save the clustering result
                # torch.save(cluster_result,os.path.join(args.exp_dir, 'clusters_%d'%epoch))

        adjust_learning_rate(optimizer, epoch, args)


        train(moco_train_loader, model, criterion, optimizer, epoch, args, cluster_result, tb_logger, pool_e_train, pool_g_train)

         #if (epoch+1)%5==0:
             #val_retrieval(moco_val_loader, model, epoch, args, tb_logger, pool_e_val, pool_g_val)

        if (epoch+1)%100==0:
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optimizer' : optimizer.state_dict(),
            }, is_best=False, filename='./tb_logs/{}/checkpoint_{}.pth.tar'.format(args.exp_dir, str(epoch)))



# Cell

def train(train_loader, model, criterion, optimizer, epoch, args, cluster_result=None, tb_logger=None, pool_e=None, pool_g=None):
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    scene_losses = AverageMeter('Scene Loss', ':.4e')
    view_losses = AverageMeter('View Loss', ':.4e')
    acc_inst_scene = AverageMeter('Acc@Inst', ':6.2f')
    acc_inst_view = AverageMeter('Acc@Inst', ':6.2f')
    acc_proto = AverageMeter('Acc@Proto', ':6.2f')

    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, acc_inst_scene, acc_inst_view,  acc_proto],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()
    for i, (feed_dict_q, feed_dict_k, metadata) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)


        ''' metric_learning_scene '''
        # compute output
        index = metadata["index"]
        output_scene, target_scene, output_proto, target_proto = model(feed_dict_q, feed_dict_k, metadata, cluster_result=cluster_result, index=index, forward_type="scene")

        # InfoNCE loss
        scene_loss = criterion(output_scene, target_scene)

        # ProtoNCE loss
        if output_proto is not None:
            loss_proto = 0
            for proto_out,proto_target in zip(output_proto, target_proto):
                loss_proto += criterion(proto_out, proto_target)
                accp = accuracy(proto_out, proto_target)[0]
                acc_proto.update(accp[0], args.batch_size)

            # average loss across all sets of prototypes
            loss_proto /= len(args.num_cluster)
            scene_loss += loss_proto

        acc = accuracy(output_scene, target_scene)[0]
        acc_inst_scene.update(acc[0], args.batch_size)

        ''' metric_learning_view '''
        # compute output
        index = metadata["index"]
        feed_dict_n_lists = sample_same_scene_negs(feed_dict_q, feed_dict_k, metadata, args.hyp_N, 2)[0]

        output_view, target_view, _, _ = model(feed_dict_q, feed_dict_k, metadata, feed_dicts_N=feed_dict_n_lists, forward_type="view")

        # InfoNCE loss
        view_loss = criterion(output_view, target_view)

        acc = accuracy(output_view, target_view)[0]
        acc_inst_view.update(acc[0], args.batch_size)

        loss = args.scene_wt*scene_loss + args.view_wt*view_loss

        scene_losses.update(scene_loss.item(), args.batch_size)
        view_losses.update(view_loss.item(), args.batch_size)
        losses.update(loss.item(), args.batch_size)


        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # store to pool
        store_to_pool(pool_e, pool_g, feed_dict_q, feed_dict_k, metadata, model, args)
        model.train()


        if i % args.print_freq == 0:
            progress.display(i)

    print("Logging to TB....")
    tb_logger.add_scalar('Train Acc Inst', acc_inst_scene.avg, epoch)
    tb_logger.add_scalar('Train Acc Inst', acc_inst_view.avg, epoch)
    tb_logger.add_scalar('Train Acc Prototype', acc_proto.avg, epoch)
    tb_logger.add_scalar('Scene Loss', scene_losses.avg, epoch)
    tb_logger.add_scalar('View Loss', view_losses.avg, epoch)
    tb_logger.add_scalar('Train Total Loss', losses.avg, epoch)

#     if epoch % args.ret_freq == 0:
#         figures = random_retrieve_topk(args, pool_e, pool_g, imgs_to_view=5)
#         tb_logger.add_figure('Train Top10 Retrieval', figures, epoch)

# Cell

def val_retrieval(val_loader, model, epoch, args, tb_logger=None, pool_e=None, pool_g=None):
    model.eval()
    for i, (feed_dict_q, feed_dict_k, metadata) in enumerate((val_loader)):
        with torch.no_grad():
            feat_q = model(feed_dict_q, None, metadata, is_eval=True)
            feat_k = model(feed_dict_k, None, metadata, is_eval=True)

            dim1 = feat_q.shape[0]
            img_q = torch.zeros([dim1, 3, 256, 256])
            img_k = torch.zeros([dim1, 3, 256, 256])


            if args.mode=='node':
                cnt = 0

                for b in range(feed_dict_q["objects_boxes"].shape[0]//args.hyp_N):
                    for s in range(args.hyp_N):
                        img_q[cnt] = feed_dict_q["images"][b]
                        img_k[cnt] = feed_dict_k["images"][b]
                        cnt += 1

                pool_e.update(feat_q, img_q, feed_dict_q["objects_boxes"], None)
                pool_g.update(feat_k, img_k, feed_dict_k["objects_boxes"], None)

            else:
                dim1 = feat_q.shape[0]
                subj_q = torch.zeros([dim1, 4])
                subj_k = torch.zeros([dim1, 4])
                obj_q = torch.zeros([dim1, 4])
                obj_k = torch.zeros([dim1, 4])

                cnt = 0
                for b in range(feed_dict_q["objects_boxes"].shape[0]//args.hyp_N):
                    for s in range(args.hyp_N):
                        for o in range(args.hyp_N):
                            img_q[cnt] = feed_dict_q["images"][b]
                            img_k[cnt] = feed_dict_k["images"][b]
                            cnt += 1

                cnt = 0
                for b in range(feed_dict_q["objects_boxes"].shape[0]//args.hyp_N):
                    for s in range(args.hyp_N):
                        for o in range(args.hyp_N):
                            start_idx = b*args.hyp_N
                            subj_q[cnt] = feed_dict_q["objects_boxes"][start_idx + s]
                            obj_q[cnt] = feed_dict_q["objects_boxes"][start_idx + o]
                            cnt += 1


                cnt = 0
                for b in range(feed_dict_q["objects_boxes"].shape[0]//args.hyp_N):
                    for s in range(args.hyp_N):
                        for o in range(args.hyp_N):
                            start_idx = b*args.hyp_N
                            subj_k[cnt] = feed_dict_k["objects_boxes"][start_idx + s]
                            obj_k[cnt] = feed_dict_k["objects_boxes"][start_idx + o]
                            cnt += 1

                pool_e.update(feat_q, img_q, subj_q, obj_q)
                pool_g.update(feat_k, img_k, subj_k, obj_k)

    figures = random_retrieve_topk(args, pool_e, pool_g, imgs_to_view=5)
    tb_logger.add_figure('Validation Top10 Retrieval', figures, epoch)

    return