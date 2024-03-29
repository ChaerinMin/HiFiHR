import pickle
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import os
import math
import time
import copy 

import numpy as np
import pytorch3d
from pytorch3d.renderer import (
    RasterizationSettings,
    MeshRenderer, 
    MeshRasterizer,
    HardPhongShader,
    Materials
)
from pytorch3d.renderer.lighting import DirectionalLights
import pytorch3d.renderer as p3d_renderer
from .network.res_encoder import ResEncoder, HandEncoder, LightEstimator
from .network.effnet_encoder import EffiEncoder
from .utils.NIMBLE_model.myNIMBLELayer import MyNIMBLELayer
from .utils.traineval_util import Mano2Frei, trans_proj_j2d
from .utils.my_mano import MyMANOLayer
from .utils.Freihand_GNN_mano.mano_network_PCA import YTBHand
from .utils.Freihand_GNN_mano.Freihand_trainer_mano_fullsup import dense_pose_Trainer
ytbHand_trainer = dense_pose_Trainer(None, None)


class Model(nn.Module):
    def __init__(self, ifRender, device, if_4c, hand_model, use_mean_shape, pretrain, root_id=9, root_id_nimble=11, ifLight=True):
        super(Model, self).__init__()
        self.hand_model = hand_model
        self.root_id = root_id
        self.root_id_nimble = root_id_nimble
        if hand_model == 'mano_new':
            self.ytbHand = YTBHand(None, None, use_pca=True, pca_comps=48)
            return

        if pretrain == 'hr18sv2':
            self.features_dim = 1024 # for HRnet
            self.low_feat_dim = 512 # not sure
            self.base_encoder = ResEncoder(pretrain=pretrain, if_4c=if_4c)
        elif pretrain in ['res18', 'res50', 'res101']:
            self.features_dim = 2048
            self.low_feat_dim = 512
            self.base_encoder = ResEncoder(pretrain=pretrain, if_4c=if_4c)
        elif pretrain == 'effb3':
            self.features_dim = 1536
            self.low_feat_dim = 32
            self.base_encoder = EffiEncoder(pretrain=pretrain)
        
        if hand_model == 'nimble':
            self.ncomps = [20, 30, 10] # shape, pose, tex respectively.
            self.hand_layer = MyNIMBLELayer(ifRender, device, shape_ncomp=self.ncomps[0], pose_ncomp=self.ncomps[1], tex_ncomp=self.ncomps[2])
        elif hand_model == 'mano':
            self.ncomps = [10, 48, None] # shape, pose, no texture.
            self.hand_layer = MyMANOLayer(ifRender, device, shape_ncomp=self.ncomps[0], pose_ncomp=self.ncomps[1], tex_ncomp=self.ncomps[2])
        # elif hand_model == 'smplxarm':
        #     self.ncomps = []
        #     self.hand_layer = MySMPLXARM('hand_models/smplx/models/smplx',gender='neutral', use_face_contour=False,
        #                         num_betas=10, use_pca=False,
        #                         num_expression_coeffs=10,
        #                         ext='npz')    

        self.hand_encoder = HandEncoder(hand_model=hand_model, ncomps=self.ncomps, in_dim=self.features_dim, ifRender=ifRender, use_mean_shape=use_mean_shape)

        MANO_file = 'assets/mano/MANO_RIGHT.pkl'
        dd = pickle.load(open(MANO_file, 'rb'),encoding='latin1')
        self.mano_face = Variable(torch.from_numpy(np.expand_dims(dd['f'],0).astype(np.int16)).to(device=device))

        self.ifRender = ifRender
        self.ifLight = ifLight
        self.aa_factor = 3
        # Renderer
        if self.ifRender:
            # Define a RGB renderer with HardPhongShader in pytorch3d
            raster_settings_soft = RasterizationSettings(
                image_size=224 * self.aa_factor, 
                blur_radius=0.0, 
                faces_per_pixel=1, 
            )
            materials = Materials(
                # ambient_color=((0.9, 0.9, 0.9),),
                diffuse_color=((0.8, 0.8, 0.8),),
                specular_color=((0.2, 0.2, 0.2),),
                shininess=30,
                device=device,
            )

            # Differentiable soft renderer with SoftPhongShader
            self.renderer_p3d = MeshRenderer(
                rasterizer=MeshRasterizer(
                    raster_settings=raster_settings_soft
                ),
                shader=HardPhongShader(
                    materials=materials,
                    device=device, 
                ),
            )

        if self.ifLight:
            self.light_estimator = LightEstimator(self.low_feat_dim)


    def forward(self, dat_name, mode_train, images, Ks=None):
        if self.hand_model == 'mano_new':
            pred = self.ytbHand(images)
            outputs = {
                'pose_params': pred['theta'],
                'shape_params': pred['beta'],
                'verts': pred['mesh']
                }
            return outputs
        device = images.device
        batch_size = images.shape[0]
        # Use base_encoder to extract features
        # low_features, features = self.base_encoder(images) # [b, 512, 14, 14], [b,1024]
        
        low_features, features = self.base_encoder(images) # [b, 512, 14, 14], [b,1024]
        # Use light_estimator to get light parameters
        if self.ifLight:
            light_params = self.light_estimator(low_features)
        
        # Use hand_encoder to get hand parameters
        hand_params  = self.hand_encoder(features)
        # hand_params = {
        #     'pose_params': pose_params, 
        #     'shape_params': shape_params, 
        #     'texture_params': texture_params, 
        #     'scale': scale, 
        #     'trans': trans, 
        #     'rot': rot # only for mano hand model
        # }

        # Use nimble_layer to get 3D hand models
        outputs = self.hand_layer(hand_params, handle_collision=False, with_root=True)
        # outputs = {
        #     'nimble_joints': bone_joints, # 25 joints
        #     'verts': skin_v, # 5990 verts
        #     'faces': None #faces,
        #     'skin_meshes': skin_v_smooth, # smoothed verts and faces
        #     'mano_verts': skin_mano_v, # 5990 -> 778 verts according to mano
        #     'textures': tex_img,
        #     'rot':rot
        # }
        outputs.update(hand_params)           
        hand_params_r = copy.deepcopy(hand_params)
        hand_params_r['pose_params'][:, :3] = 0
        outputs_r = self.hand_layer(hand_params_r, handle_collision=False, with_root=True)
        outputs['mano_verts_r'] = outputs_r['mano_verts']
        outputs['mano_joints_r'] = outputs_r['joints']

        # map nimble 25 joints to freihand 21 joints
        if self.hand_model == 'mano_new':
            # regress joints from verts
            vertice_pred_list = outputs['verts']
            outputs['joints'] = ytbHand_trainer.xyz_from_vertice(vertice_pred_list[-1]).permute(1,0,2)
        elif self.hand_model == 'mano':
            # regress joints from verts
            vertice_pred_list = outputs['mano_verts']
            outputs['joints'] = ytbHand_trainer.xyz_from_vertice(vertice_pred_list).permute(1,0,2)
        else: # nimble
            # Mano joints map to Frei joints
            outputs['joints'] = Mano2Frei(outputs['joints'])
        

        # ** offset positions relative to root.
        if dat_name == 'HO3D' and not mode_train:
            # they only provide wrist joint (0) in test set
            pred_root_xyz = outputs['joints'][:, 0, :].unsqueeze(1)
        else:
            pred_root_xyz = outputs['joints'][:, self.root_id, :].unsqueeze(1)
        outputs['joints'] = outputs['joints'] - pred_root_xyz
        outputs['mano_verts'] = outputs['mano_verts'] - pred_root_xyz
        if self.hand_model == 'nimble':
            if dat_name == 'HO3D' and not mode_train:
                pred_root_xyz = outputs['nimble_joints'][:, 0, :].unsqueeze(1)
            else:
                pred_root_xyz = outputs['nimble_joints'][:, self.root_id_nimble, :].unsqueeze(1)
            outputs['xyz'] = outputs['nimble_joints']
            outputs['nimble_joints'] = outputs['nimble_joints'] - pred_root_xyz
        mano_center = outputs['mano_joints_r'][:, 5, :].unsqueeze(1)
        outputs['mano_verts_r'] = outputs['mano_verts_r'] - mano_center
        outputs['mano_joints_r'] = outputs['mano_joints_r'] - mano_center


        # Render image
        # move to the root relative coord. 
        # verts = verts - pred_root_xyz + root_xyz
        verts_num = outputs['skin_meshes']._num_verts_per_mesh[0]
        outputs['skin_meshes'].offset_verts_(-pred_root_xyz.repeat(1, verts_num, 1).view(verts_num*batch_size, 3))
        root_xyz = torch.tensor([0.0142, 0.0153, 0.9144]).unsqueeze(0).unsqueeze(0).to(pred_root_xyz.device)  # hifihr assumption
        outputs['skin_meshes'].offset_verts_(root_xyz.repeat(1, verts_num, 1).view(verts_num*batch_size, 3))
        if self.ifRender:
            # set up renderer parameters
            # k_44 = torch.eye(4).unsqueeze(0).repeat(batch_size, 1, 1)
            # k_44[:, :3, :4] = Ks
            # cameras = p3d_renderer.cameras.PerspectiveCameras(K=k_44, device=device, in_ndc=False, image_size=((224,224),)) # R and t are identity and zeros by default

            # get ndc fx, fy, cx, cy from Ks
            fcl, prp = self.get_ndc_fx_fy_cx_cy(Ks)
            cameras = p3d_renderer.cameras.PerspectiveCameras(focal_length=-fcl, 
                                                              principal_point=prp,
                                                              device=device) # R and t are identity and zeros by default
            if self.ifLight:
                lighting = DirectionalLights(diffuse_color=light_params['colors'], # N, 3
                                            direction=light_params['directions'], # N, 3 
                                            device=device)
                outputs['light_params'] = {k:v.detach().cpu() for k, v in light_params.items()}
            else:
                lighting = p3d_renderer.lighting.PointLights(
                    # ambient_color=((1.0, 1.0, 1.0),),
                    # diffuse_color=((0.0, 0.0, 0.0),),
                    # specular_color=((0.0, 0.0, 0.0),),
                    # location=((0.0, 0.0, 0.0),),
                    device=device,
                )
                outputs['light_params'] = None
            

            # render the image
            rendered_images = self.renderer_p3d(outputs['skin_meshes'], cameras=cameras, lights=lighting)
            # average pooling to downsample the rendered image (anti-aliasing)
            rendered_images = rendered_images.permute(0, 3, 1, 2)  # NHWC -> NCHW
            rendered_images = F.avg_pool2d(rendered_images, kernel_size=self.aa_factor, stride=self.aa_factor)
            # rendered_images = rendered_images.permute(0, 2, 3, 1)  # NCHW -> NHWC

            # import torchvision
            # torchvision.utils.save_image(rendered_images[...,:3][1].permute(2,0,1),"test.png")

            outputs['re_img'] = rendered_images[:, :3, :, :] # the last dim is alpha
            outputs['re_sil'] = rendered_images[:, 3:4, :, :] # [B, 1, w, h]. the last dim is alpha
            outputs['re_sil'][outputs['re_sil'] > 0] = 255  # Binarize segmentation mask
            outputs['maskRGBs'] = images.mul((outputs['re_sil']>0).float().repeat(1,3,1,1))
            
        
        # add mano faces to outputs (used in losses)
        outputs['mano_faces'] = self.mano_face.repeat(batch_size, 1, 1)

        return outputs
    
    # get ndc fx, fy, cx, cy from Ks
    def get_ndc_fx_fy_cx_cy(self, Ks):
        ndc_fx = Ks[:, 0, 0] * 2 / 224.0
        ndc_fy = Ks[:, 1, 1] * 2 / 224.0
        ndc_px = - (Ks[:, 0, 2] - 112.0) * 2 / 224.0
        ndc_py = - (Ks[:, 1, 2] - 112.0) * 2 / 224.0
        focal_length = torch.stack([ndc_fx, ndc_fy], dim=-1)
        principal_point = torch.stack([ndc_px, ndc_py], dim=-1)
        return focal_length, principal_point
        