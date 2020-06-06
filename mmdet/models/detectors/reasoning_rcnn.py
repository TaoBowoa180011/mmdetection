from __future__ import division

import torch
import torch.nn as nn

from .base import BaseDetector
from .test_mixins import RPNTestMixin
from .. import builder
from ..registry import DETECTORS
from mmdet.core import (assign_and_sample, bbox2roi, bbox2result, multi_apply,build_assigner, build_sampler,
                        merge_aug_masks)

import numpy as np
import pickle
from mmdet.ops import ConvModule
import torch.nn.functional as F

@DETECTORS.register_module
class ReasoningRCNN(BaseDetector, RPNTestMixin):

    def __init__(self,
                 num_stages,
                 backbone,
                 neck=None,
                 upper_neck=None,
                 rpn_head=None,
                 bbox_roi_extractor=None,
                 bbox_head=None,
                 mask_roi_extractor=None,
                 mask_head=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None,
                 adj_gta=None,
                 adj_gtr=None,
                 graph_out_channels=256,
                 graph_hidden_channels=512,
                 norm_cfg=None,
                 roi_feat_size=7,
                 shared_num_fc=2):
        assert bbox_roi_extractor is not None
        assert bbox_head is not None
        super(ReasoningRCNN, self).__init__()

        self.num_stages = num_stages
        self.backbone = builder.build_backbone(backbone)

        if neck is not None:
            self.neck = builder.build_neck(neck)
        else:
            assert upper_neck is not None

        if rpn_head is not None:
            self.rpn_head = builder.build_head(rpn_head)

        if upper_neck is not None:
            if isinstance(upper_neck, list):
                self.upper_neck = nn.ModuleList()
                assert len(upper_neck) == self.num_stages
                for neck in upper_neck:
                    self.upper_neck.append(builder.build_upper_neck(neck))
            else:
                self.upper_neck = builder.build_upper_neck(upper_neck)

        if bbox_head is not None:
            self.bbox_roi_extractor = nn.ModuleList()
            self.bbox_head = nn.ModuleList()
            if not isinstance(bbox_roi_extractor, list):
                bbox_roi_extractor = [
                    bbox_roi_extractor for _ in range(num_stages)
                ]
            if not isinstance(bbox_head, list):
                bbox_head = [bbox_head for _ in range(num_stages)]
            assert len(bbox_roi_extractor) == len(bbox_head) == self.num_stages
            for roi_extractor, head in zip(bbox_roi_extractor, bbox_head):
                self.bbox_roi_extractor.append(
                    builder.build_roi_extractor(roi_extractor))
                self.bbox_head.append(builder.build_head(head))

        if mask_head is not None:
            self.mask_head = nn.ModuleList()
            if not isinstance(mask_head, list):
                mask_head = [mask_head for _ in range(num_stages)]
            assert len(mask_head) == self.num_stages
            for head in mask_head:
                self.mask_head.append(builder.build_head(head))
            if mask_roi_extractor is not None:
                self.mask_roi_extractor = nn.ModuleList()
                if not isinstance(mask_roi_extractor, list):
                    mask_roi_extractor = [
                        mask_roi_extractor for _ in range(num_stages)
                    ]
                assert len(mask_roi_extractor) == self.num_stages
                for roi_extractor in mask_roi_extractor:
                    self.mask_roi_extractor.append(
                        builder.build_roi_extractor(roi_extractor))
                self.share_roi_extractor = False
            else:
                self.share_roi_extractor = True
                self.mask_roi_extractor = self.bbox_roi_extractor

        self.norm_cfg = norm_cfg
         
        self.graphr_coeffience = nn.Parameter(torch.Tensor([1.00]),requires_grad = False)        
        self.grapha_coeffience = nn.Parameter(torch.Tensor([0.25]),requires_grad = False)
        if adj_gta is not None:
            adj_gta = pickle.load(open(adj_gta, 'rb'))
            adj_gta = np.float32(adj_gta)
            adj_gta = adj_gta - np.identity(np.size(adj_gta,0),dtype=np.float32)
            adj_gta = adj_gta[1:,1:]
            #normalize
            adj_gta = torch.from_numpy(adj_gta)
            adj_gta = F.normalize(adj_gta,p=1,dim=1)

            grapha = torch.zeros(adj_gta.size(0),adj_gta.size(0))
            grapha =  self.grapha_coeffience * adj_gta + torch.eye(adj_gta.size(0))
            graph_a = torch.zeros(adj_gta.size(0)+1,adj_gta.size(0)+1)
            graph_a[1:,1:] = grapha[:,:]
            self.graph_a = nn.Parameter(graph_a, requires_grad=False)
            print(self.graph_a)
        if adj_gtr is not None:
            adj_gtr = pickle.load(open(adj_gtr, 'rb'))
            adj_gtr = np.float32(adj_gtr)
            adj_gtr = adj_gtr - np.identity(np.size(adj_gtr,0),dtype=np.float32)
            adj_gtr = adj_gtr[1:,1:]
            adj_gtr = torch.from_numpy(adj_gtr)
            adj_gtr = F.normalize(adj_gtr,p=1,dim=1)

            graphr = torch.zeros(adj_gtr.size(0),adj_gtr.size(0))
            graphr =  self.graphr_coeffience * adj_gtr + torch.eye(adj_gtr.size(0))
            graph_r = torch.zeros(adj_gtr.size(0)+1,adj_gtr.size(0)+1)
            graph_r[1:,1:] = graphr[:,:]
            self.graph_r = nn.Parameter(graph_r, requires_grad=False)
            print(self.graph_r)

        # init cmp attention
        self.cmp_attention = nn.ModuleList()
        self.cmp_attention.append(
            ConvModule(1024, 1024 // 16,
                       3, stride=2, padding=1, norm_cfg=self.norm_cfg, bias=self.norm_cfg is None))
        self.cmp_attention.append(
            nn.Linear(1024 // 16, bbox_head[0]['in_channels'] + 1)) # box_head[0]['in_channels'] = 1024
        # init GCN weights
        self.graph_out_channels = graph_out_channels #256
        self.graph_hidden_channels = graph_hidden_channels#512
        self.graph_weight_fc1 = nn.Linear(bbox_head[0]['in_channels'] + 1, self.graph_hidden_channels)
        self.graph_weight_fc2 = nn.Linear(self.graph_hidden_channels, self.graph_out_channels)
        self.relu = nn.ReLU(inplace=True)
        
        
        # shared upper neck
        in_channels = rpn_head['in_channels']
        if shared_num_fc > 0:
            in_channels *= (roi_feat_size * roi_feat_size)
        self.branch_fcs = nn.ModuleList()
        for i in range(shared_num_fc):
            fc_in_channels = (in_channels
                              if i == 0 else bbox_head[0]['in_channels'])
            self.branch_fcs.append(
                nn.Linear(fc_in_channels, bbox_head[0]['in_channels']))

        self.train_cfg = train_cfg
        self.test_cfg = test_cfg

        self.init_weights(pretrained=pretrained)

    @property
    def with_rpn(self):
        return hasattr(self, 'rpn_head') and self.rpn_head is not None

    def init_weights(self, pretrained=None):
        super(ReasoningRCNN, self).init_weights(pretrained)
        self.backbone.init_weights(pretrained=pretrained)
        if self.with_neck:
            if isinstance(self.neck, nn.Sequential):
                for m in self.neck:
                    m.init_weights()
            else:
                self.neck.init_weights()
        if self.with_rpn:
            self.rpn_head.init_weights()
        for i in range(self.num_stages):
            if self.with_bbox:
                self.bbox_roi_extractor[i].init_weights()
                self.bbox_head[i].init_weights()
            
            if self.with_mask:
                self.mask_head[i].init_weights()
                if self.with_mask_roi_extractor:
                    self.mask_roi_extractor[i].init_weights()

    def extract_feat(self, img):
        x = self.backbone(img)
        if self.with_neck:
            x = self.neck(x)
        return x

    def forward_upper_neck(self, x, stage):
        if self.with_share_upper_neck:
            x = self.upper_neck(x)
        elif self.with_unshare_upper_neck:
            x = self.upper_neck[stage](x)
        return x

    def forward_train(self,
                      img,
                      img_meta,
                      gt_bboxes,
                      gt_labels,
                      gt_bboxes_ignore = None,
                      gt_masks=None,
                      proposals=None):
        x = self.extract_feat(img)
        
        # precmp attention
        if len(x) > 1:
            base_feat = []
            for b_f in x[1:]:
                base_feat.append(
                    F.interpolate(b_f, scale_factor=(x[2].size(2) / b_f.size(2), x[2].size(3) / b_f.size(3))))
            base_feat = torch.cat(base_feat, 1)
        else:
            base_feat = torch.cat(x, 1) 
        #base_feat.size = [2,1024,38,58]

        #base_feat = con2d(1064,64,3)(base_feat) ->[2,64,19,29]
        #base_feat = global_avarage_pooling(base_feat)->[2,64]
        #base_feat = linear(64,1025)(base_feat) ->[2,1025]
        for ops in self.cmp_attention:
            base_feat = ops(base_feat) 
            if len(base_feat.size()) > 2:
                base_feat = base_feat.mean(3).mean(2)
            else:
                base_feat = self.relu(base_feat)      #[2,1025]   
        
        losses = dict()

        # RPN forward and loss
        if self.with_rpn:
            rpn_outs = self.rpn_head(x)
            rpn_loss_inputs = rpn_outs + (gt_bboxes, img_meta,
                                          self.train_cfg.rpn)
            rpn_losses = self.rpn_head.loss(
                *rpn_loss_inputs, gt_bboxes_ignore=gt_bboxes_ignore)
            losses.update(rpn_losses)

            proposal_inputs = rpn_outs + (img_meta, self.test_cfg.rpn)
            proposal_list = self.rpn_head.get_bboxes(*proposal_inputs)
        else:
            proposal_list = proposals

        for i in range(self.num_stages):
            rcnn_train_cfg = self.train_cfg.rcnn[i]
            lw = self.train_cfg.stage_loss_weights[i]

            # add reasoning process
            if i > 0:
                # 1.build global semantic pool [21,1025]
                global_semantic_pool = torch.cat((bbox_head.fc_cls.weight,
                                                  bbox_head.fc_cls.bias.unsqueeze(1)), 1).detach()
    
                # 2.compute graph attention | matrix multiply([2,1025] , [1025,21]) ->[2,21] ->softmax(1)->[2,21] 
                attention_map = nn.Softmax(1)(torch.mm(base_feat, torch.transpose(global_semantic_pool, 0, 1)))
                
                GrM= torch.mm(self.graph_r, global_semantic_pool)   # C*D
                GrMW = self.graph_weight_fc1(GrM)               # C*512      
                H1 = self.relu(GrMW)                 
               
                #               [2,21,1] * [1,21,512] -> [2,21,512]
                alpha_H1 = attention_map.unsqueeze(-1) * torch.mm(self.graph_a, H1).unsqueeze(0)
                alpha_H1 = alpha_H1.view(-1, alpha_H1.size(-1)) #[42,512]
               
                H2 = self.graph_weight_fc2(alpha_H1)  # 21*256
                H2 = self.relu(H2) 

                n_classes = bbox_head.fc_cls.weight.size(0) #21
                # cls_score = fc_cls(bbox_feat) fc_cls=linear(1024,21)(bbox_feat = [1024,1024]) -> [1024,21]
                # cls_prob = [2,512,21]                 
                cls_prob = nn.Softmax(1)(cls_score).view(len(img_meta), -1, n_classes) 
                # bmm([2,512,21],[2,21,256])->[2,512,256]
                enhanced_feat = torch.bmm(cls_prob, H2.view(len(img_meta), -1, self.graph_out_channels))#[2,512,256]
                enhanced_feat = enhanced_feat.view(-1, self.graph_out_channels) #[1024,256]
                
            # assign gts and sample proposals
            if self.with_bbox or self.with_mask:
                bbox_assigner = build_assigner(self.train_cfg.rcnn[i].assigner)
                bbox_sampler = build_sampler(
                    self.train_cfg.rcnn[i].sampler, context=self)
                num_imgs = img.size(0)
                if gt_bboxes_ignore is None:
                    gt_bboxes_ignore = [None for _ in range(num_imgs)]
                sampling_results = []
                for num in range(num_imgs):
                    assign_result = bbox_assigner.assign(proposal_list[num],
                                                     gt_bboxes[num],
                                                     gt_bboxes_ignore[num],
                                                     gt_labels[num])
                    sampling_result = bbox_sampler.sample(
                        assign_result,
                        proposal_list[num],
                        gt_bboxes[num],
                        gt_labels[num],
                        feats=[lvl_feat[num][None] for lvl_feat in x])
                    sampling_results.append(sampling_result)

            # bbox head forward and loss
            if self.with_bbox:
                bbox_roi_extractor = self.bbox_roi_extractor[i]
                bbox_head = self.bbox_head[i]

                rois = bbox2roi([res.bboxes for res in sampling_results])
                bbox_feats = bbox_roi_extractor(x[:bbox_roi_extractor.num_inputs], # bbox_feats = [1024,256,7,7] 1024 =512+512 Nr=512, b =2
                                            rois)
                
                # without upperneck/shared_head
                bbox_feats = bbox_feats.view(bbox_feats.size(0), -1) # bbox_feats = [1024,12544]
                for fc in self.branch_fcs: #linear(12544,1024),linear(1024,1024)
                    bbox_feats = self.relu(fc(bbox_feats))
                #bbox_feats = [1024,1024]
                # cat with enhanced feature
                if i > 0:
                    bbox_feats = torch.cat([bbox_feats, enhanced_feat], 1)

                cls_score, bbox_pred = bbox_head(bbox_feats)

                bbox_targets = bbox_head.get_target(sampling_results, gt_bboxes,
                                                gt_labels, self.train_cfg.rcnn[i])
                loss_bbox = bbox_head.loss(cls_score, bbox_pred, *bbox_targets)
               
                for name, value in loss_bbox.items():
                    losses['s{}.{}'.format(
                    i, name)] = (value * lw if 'loss' in name else value)
            # mask head forward and loss
            if self.with_mask:
                if self.with_mask_roi_extractor:
                    mask_roi_extractor = self.mask_roi_extractor[i]
                    pos_rois = bbox2roi(
                        [res.pos_bboxes for res in sampling_results])
                    mask_feats = mask_roi_extractor(
                        x[:mask_roi_extractor.num_inputs], pos_rois)
                    mask_feats = self.forward_upper_neck(mask_feats, i)
                else:
                    pos_inds = (rois_index == 0)
                    mask_feats = bbox_feats[pos_inds]
                mask_head = self.mask_head[i]
                mask_pred = mask_head(mask_feats)
                mask_targets = mask_head.get_target(sampling_results, gt_masks,
                                                    rcnn_train_cfg)
                pos_labels = torch.cat(
                    [res.pos_gt_labels for res in sampling_results])
                loss_mask = mask_head.loss(mask_pred, mask_targets, pos_labels)
                for name, value in loss_mask.items():
                    losses['s{}.{}'.format(
                        i, name)] = (value * lw if 'loss' in name else value)

            # refine bboxes
            if i < self.num_stages - 1:
                pos_is_gts = [res.pos_is_gt for res in sampling_results]
                roi_labels = bbox_targets[0]  # bbox_targets is a tuple
                with torch.no_grad():
                    proposal_list = bbox_head.refine_bboxes(
                        rois, roi_labels, bbox_pred, pos_is_gts, img_meta)

        return losses

    def simple_test(self, img, img_meta, proposals=None, rescale=False):
        x = self.extract_feat(img)

        # precmp attention
        if len(x) > 1:
            base_feat = []
            for b_f in x[1:]:
                base_feat.append(
                    F.interpolate(b_f, scale_factor=(x[2].size(2) / b_f.size(2), x[2].size(3) / b_f.size(3))))
            base_feat = torch.cat(base_feat, 1)
        else:
            base_feat = torch.cat(x, 1)

        for ops in self.cmp_attention:
            base_feat = ops(base_feat)
            if len(base_feat.size()) > 2:
                base_feat = base_feat.mean(3).mean(2)
            else:
                base_feat = self.relu(base_feat)

        proposal_list = self.simple_test_rpn(
            x, img_meta, self.test_cfg.rpn) if proposals is None else proposals

        img_shape = img_meta[0]['img_shape']
        ori_shape = img_meta[0]['ori_shape']
        scale_factor = img_meta[0]['scale_factor']

        # "ms" in variable names means multi-stage
        ms_bbox_result = {}
        ms_segm_result = {}
        ms_scores = []
        rcnn_test_cfg = self.test_cfg.rcnn

        rois = bbox2roi(proposal_list) # roi shape (n, 5), [batch_ind, x1, y1, x2, y2]  
        for i in range(self.num_stages):
            # add reasoning process
            if i > 0:
                # transform CxC classes graph to region
                # 1.build global semantic pool
                global_semantic_pool = torch.cat((bbox_head.fc_cls.weight,
                                                  bbox_head.fc_cls.bias.unsqueeze(1)), 1).detach()
                # 2.compute graph attention
                attention_map = nn.Softmax(1)(torch.mm(base_feat, torch.transpose(global_semantic_pool, 0, 1)))
                # 3.adaptive global reasoning
                alpha_em = attention_map.unsqueeze(-1) * torch.mm(self.adj_gt, global_semantic_pool).unsqueeze(0)
                alpha_em = alpha_em.view(-1, global_semantic_pool.size(-1))
                alpha_em = self.graph_weight_fc(alpha_em)
                alpha_em = self.relu(alpha_em)
                n_classes = bbox_head.fc_cls.weight.size(0)
                cls_prob = nn.Softmax(1)(cls_score).view(len(img_meta), -1, n_classes)
                enhanced_feat = torch.bmm(cls_prob, alpha_em.view(len(img_meta), -1, self.graph_out_channels))
                enhanced_feat = enhanced_feat.view(-1, self.graph_out_channels)

            bbox_roi_extractor = self.bbox_roi_extractor[i]
            bbox_head = self.bbox_head[i]

            bbox_feats = bbox_roi_extractor(
                x[:len(bbox_roi_extractor.featmap_strides)], rois)
            # bbox_feats = self.forward_upper_neck(bbox_feats, i)
            # without upperneck
            bbox_feats = bbox_feats.view(bbox_feats.size(0), -1)
            for fc in self.branch_fcs:
                bbox_feats = self.relu(fc(bbox_feats))
            # cat with enhanced feature
            if i > 0:
                bbox_feats = torch.cat([bbox_feats, enhanced_feat], 1)

            cls_score, bbox_pred = bbox_head(bbox_feats)
            ms_scores.append(cls_score)

            if self.test_cfg.keep_all_stages:
                det_bboxes, det_labels = bbox_head.get_det_bboxes(
                    rois,
                    cls_score,
                    bbox_pred,
                    img_shape,
                    scale_factor,
                    rescale=rescale,
                    cfg=rcnn_test_cfg)
                bbox_result = bbox2result(det_bboxes, det_labels,
                                          bbox_head.num_classes)
                ms_bbox_result['stage{}'.format(i)] = bbox_result

                if self.with_mask:
                    if self.with_mask_roi_extractor:
                        mask_roi_extractor = self.mask_roi_extractor[i]
                    else:
                        mask_roi_extractor = self.bbox_roi_extractor[i]
                    mask_head = self.mask_head[i]
                    if det_bboxes.shape[0] == 0:
                        segm_result = [
                            [] for _ in range(mask_head.num_classes - 1)
                        ]
                    else:
                        _bboxes = (det_bboxes[:, :4] * scale_factor
                                   if rescale else det_bboxes)
                        mask_rois = bbox2roi([_bboxes])
                        mask_feats = mask_roi_extractor(
                            x[:len(mask_roi_extractor.featmap_strides)],
                            mask_rois)
                        mask_feats = self.forward_upper_neck(mask_feats, i)
                        mask_pred = mask_head(mask_feats)
                        segm_result = mask_head.get_seg_masks(
                            mask_pred, _bboxes, det_labels, rcnn_test_cfg,
                            ori_shape, scale_factor, rescale)
                    ms_segm_result['stage{}'.format(i)] = segm_result

            if i < self.num_stages - 1:
                bbox_label = cls_score.argmax(dim=1)
                rois = bbox_head.regress_by_class(rois, bbox_label, bbox_pred,
                                                  img_meta[0])

        cls_score = sum(ms_scores) / self.num_stages
        det_bboxes, det_labels = self.bbox_head[-1].get_det_bboxes(
            rois,
            cls_score,
            bbox_pred,
            img_shape,
            scale_factor,
            rescale=rescale,
            cfg=rcnn_test_cfg)
        bbox_result = bbox2result(det_bboxes, det_labels,
                                  self.bbox_head[-1].num_classes)
        ms_bbox_result['ensemble'] = bbox_result

        if self.with_mask:
            if det_bboxes.shape[0] == 0:
                segm_result = [
                    [] for _ in range(self.mask_head[-1].num_classes - 1)
                ]
            else:
                _bboxes = (det_bboxes[:, :4] * scale_factor
                           if rescale else det_bboxes)
                mask_rois = bbox2roi([_bboxes])
                aug_masks = []
                for i in range(self.num_stages):
                    if self.with_mask_roi_extractor:
                        mask_roi_extractor = self.mask_roi_extractor[i]
                    else:
                        mask_roi_extractor = self.bbox_roi_extractor[i]
                    mask_feats = mask_roi_extractor(
                        x[:len(mask_roi_extractor.featmap_strides)], mask_rois)
                    mask_feats = self.forward_upper_neck(mask_feats, i)
                    mask_pred = self.mask_head[i](mask_feats)
                    aug_masks.append(mask_pred.sigmoid().cpu().numpy())
                merged_masks = merge_aug_masks(aug_masks,
                                               [img_meta] * self.num_stages,
                                               self.test_cfg.rcnn)
                segm_result = self.mask_head[-1].get_seg_masks(
                    merged_masks, _bboxes, det_labels, rcnn_test_cfg,
                    ori_shape, scale_factor, rescale)
            ms_segm_result['ensemble'] = segm_result

        if not self.test_cfg.keep_all_stages:
            if self.with_mask:
                results = (ms_bbox_result['ensemble'],
                           ms_segm_result['ensemble'])
            else:
                results = ms_bbox_result['ensemble']
        else:
            if self.with_mask:
                results = {
                    stage: (ms_bbox_result[stage], ms_segm_result[stage])
                    for stage in ms_bbox_result
                }
            else:
                results = ms_bbox_result['stage1']

        return results

    def aug_test(self, img, img_meta, proposals=None, rescale=False):
        raise NotImplementedError

    def show_result(self, data, result, **kwargs):
        if self.with_mask:
            ms_bbox_result, ms_segm_result = result
            if isinstance(ms_bbox_result, dict):
                result = (ms_bbox_result['ensemble'],
                          ms_segm_result['ensemble'])
        else:
            if isinstance(result, dict):
                result = result['ensemble']
        super(ReasoningRCNN, self).show_result(data, result, **kwargs)
