import torch
import numpy as np

torch.manual_seed(120)
from tqdm import tqdm
# from pytorch3d.loss import chamfer_distance
from NURBSDiff.surf_eval import SurfEval
import matplotlib.pyplot as plt
from torch.autograd import Variable
from mpl_toolkits.mplot3d import Axes3D
from matplotlib import cm
import sys
import copy
import json

from geomdl import NURBS, multi, exchange
from geomdl.visualization import VisMPL
from geomdl.exchange import export_smesh, import_smesh
# import CPU_Eval as cpu


def chamfer_distance_one_side(pred, gt, side=1):
    """
    Computes average chamfer distance prediction and groundtruth
    but is one sided
    :param pred: Prediction: B x N x 3
    :param gt: ground truth: B x M x 3
    :return:
    """
    if isinstance(pred, np.ndarray):
        pred = Variable(torch.from_numpy(pred.astype(np.float32))).cuda()

    if isinstance(gt, np.ndarray):
        gt = Variable(torch.from_numpy(gt.astype(np.float32))).cuda()

    pred = torch.unsqueeze(pred, 1)
    gt = torch.unsqueeze(gt, 2)

    diff = pred - gt
    diff = torch.sum(diff ** 2, 3)
    if side == 0:
        cd = torch.mean(torch.min(diff, 1)[0], 1)
    elif side == 1:
        cd = torch.mean(torch.min(diff, 2)[0], 1)
    cd = torch.mean(cd)
    return cd


def main():
    uEvalPtSize = 32
    vEvalPtSize = 32
    device = 'cuda'
    dataFileName = 'c5dataReg.txt'
    jsonInFileName = 'AMSurface.json'
    jsonOutFileName = "AMSurface.out.json"
    with open(jsonInFileName, 'r') as f:
        surface = json.load(f)

    dimension = 3
    degree = [surface['shape']['data']['degree_u'], surface['shape']['data']['degree_v']]
    CtrlPtsCountUV = [surface['shape']['data']['size_u'], surface['shape']['data']['size_v']]
    CtrlPtsTotal = CtrlPtsCountUV[0] * CtrlPtsCountUV[1]

    knotU = surface['shape']['data']['knotvector_u']
    knotV = surface['shape']['data']['knotvector_v']

    # CtrlPts = np.array(surface['data']['ctrlpts']['points'])

    CtrlPtsNoW = np.array(surface['shape']['data']['control_points']['points'])
    Weights = np.array(surface['shape']['data']['control_points']['weights'])
    CtrlPts = [CtrlPtsNoW, Weights]
    target = torch.from_numpy(np.genfromtxt(dataFileName, delimiter='\t', dtype=np.float32))
    mumPoints = target.cpu().shape

    layer = SurfEval(CtrlPtsCountUV[0], CtrlPtsCountUV[1], knot_u=knotU, knot_v=knotV, dimension=3,
                     p=degree[0], q=degree[1], out_dim_u=uEvalPtSize, out_dim_v=vEvalPtSize, dvc=device)

    if device == 'cuda':
        inpCtrlPts = torch.nn.Parameter(torch.from_numpy(copy.deepcopy(CtrlPtsNoW)).cuda())
        inpWeight = torch.ones(1, CtrlPtsCountUV[0], CtrlPtsCountUV[1], 1).cuda()
    else:
        inpCtrlPts = torch.nn.Parameter(torch.from_numpy(copy.deepcopy(CtrlPtsNoW)))
        inpWeight = torch.ones(1, CtrlPtsCountUV[0], CtrlPtsCountUV[1], 1)
    base_out = layer(torch.cat((inpCtrlPts.unsqueeze(0), inpWeight), axis=-1))

    BaseAreaSurf = base_out.detach().cpu().numpy().squeeze()
    EvalPoints = np.reshape(BaseAreaSurf,(uEvalPtSize*vEvalPtSize,3))
    np.savetxt("Eval.txt", EvalPoints, delimiter="\t")

    base_length_u = ((BaseAreaSurf[:-1, :-1, :] - BaseAreaSurf[1:, :-1, :]) ** 2).sum(-1).squeeze()
    base_length_v = ((BaseAreaSurf[:-1, :-1, :] - BaseAreaSurf[:-1, 1:, :]) ** 2).sum(-1).squeeze()
    surf_areas_base = np.multiply(base_length_u, base_length_v)
    surf_areas_base_torch = torch.from_numpy(surf_areas_base).cuda()
    base_length_u1 = np.sum(base_length_u[:, -1])
    base_area = np.sum(surf_areas_base)

    base_der11 = ((2 * base_out[:, 1:-1, 1:-1, :] - base_out[:, 0:-2, 1:-1, :] - base_out[:, 2:, 1:-1, :]) ** 2).sum(-1).squeeze()
    base_der22 = ((2 * base_out[:, 1:-1, 1:-1, :] - base_out[:, 1:-1, 0:-2, :] - base_out[:, 1:-1, 2:, :]) ** 2).sum(-1).squeeze()
    base_der12 = ((2 * base_out[:, 1:-1, 1:-1, :] - base_out[:, 0:-2, 1:-1, :] - base_out[:, 1:-1, 2:, :]) ** 2).sum(-1).squeeze()
    base_der21 = ((2 * base_out[:, 1:-1, 1:-1, :] - base_out[:, 1:-1, 0:-2, :] - base_out[:, 2:, 1:-1, :]) ** 2).sum(-1).squeeze()
    base_surf_curv11 = torch.max(base_der11)
    base_surf_curv22 = torch.max(base_der22)
    base_surf_curv12 = torch.max(base_der12)
    base_surf_curv21 = torch.max(base_der21)

    print('\nBase surface area: ',base_area)
    print('Max curvatures: ')
    print(base_surf_curv11.detach().cpu().numpy().squeeze(), base_surf_curv12.detach().cpu().numpy().squeeze())
    print(base_surf_curv21.detach().cpu().numpy().squeeze(), base_surf_curv22.detach().cpu().numpy().squeeze())

    opt = torch.optim.Adam(iter([inpCtrlPts]), lr=1e-1)
    #opt = torch.optim.LBFGS(iter([inpCtrlPts]), lr=0.2, max_iter=5)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(opt, milestones=[50,100,150,200,250,300], gamma=0.1)
    pbar = tqdm(range(5000))
    for i in pbar:
        
        def closure():
            opt.zero_grad()

            out = layer(torch.cat((inpCtrlPts.unsqueeze(0), weight), axis=-1))

            length_u = ((out[:, :-1, :-1, :] - out[:, 1:, :-1, :]) ** 2).sum(-1).squeeze()
            length_v = ((out[:, :-1, :-1, :] - out[:, :-1, 1:, :]) ** 2).sum(-1).squeeze()
            length_u1 = length_u[:,-1]
            surf_areas = torch.multiply(length_u, length_v)

            der11 = ((2*out[:, 1:-1, 1:-1, :] - out[:, 0:-2, 1:-1, :] - out[:, 2:, 1:-1, :]) ** 2).sum(-1).squeeze()
            der22 = ((2*out[:, 1:-1, 1:-1, :] - out[:, 1:-1, 0:-2, :] - out[:, 1:-1, 2:, :]) ** 2).sum(-1).squeeze()
            der12 = ((2*out[:, 1:-1, 1:-1, :] - out[:, 0:-2, 1:-1, :] - out[:, 1:-1, 2:, :]) ** 2).sum(-1).squeeze()
            der21 = ((2*out[:, 1:-1, 1:-1, :] - out[:, 1:-1, 0:-2, :] - out[:, 2:, 1:-1, :]) ** 2).sum(-1).squeeze()
            surf_curv11 = torch.max(der11)
            surf_curv22 = torch.max(der22)
            surf_curv12 = torch.max(der12)
            surf_curv21 = torch.max(der21)
            surf_max_curv = torch.sum(torch.tensor([surf_curv11,surf_curv22,surf_curv12,surf_curv21]))

            # lossVal = 0
            if device == 'cuda':
                lossVal = chamfer_distance_one_side(out.view(1, uEvalPtSize * vEvalPtSize, 3), target.view(1, mumPoints[0], 3).cuda())
            else:
                lossVal = chamfer_distance_one_side(out.view(1, uEvalPtSize * vEvalPtSize, 3), target.view(1, mumPoints[0], 3))

            # loss, _ = chamfer_distance(target.view(1, 360, 3), out.view(1, evalPtSize * evalPtSize, 3))
            # if (i < 100):
            #     lossVal += (1) * torch.abs(torch.sum(length_u1)-base_length_u1)

            # Local area change
            # lossVal += (1) * torch.sum(torch.abs(surf_areas - surf_areas_base_torch))
            # Total area change
            # lossVal += (1) * torch.abs(surf_areas.sum() - base_area)

            # Minimize maximum curvature
            lossVal += (10) * torch.abs(surf_max_curv)
            # Minimize length of u=1
            # lossVal += (.01) * torch.abs(torch.sum(length_u1) - base_length_u1)

            # Back propagate
            lossVal.backward(retain_graph=True)
            return lossVal

        if device == 'cuda':
            weight = torch.ones(1, CtrlPtsCountUV[0], CtrlPtsCountUV[1], 1).cuda()
        else:
            weight = torch.ones(1, CtrlPtsCountUV[0], CtrlPtsCountUV[1], 1)

        # Optimize step
        lossVal = opt.step(closure)
        scheduler.step()
        out = layer(torch.cat((inpCtrlPts.unsqueeze(0), weight), axis=-1))

        # Fixing U = 0 Ctrl Pts
        # inpCtrlPts.data[0,:,:] = torch.from_numpy(copy.deepcopy(CtrlPtsNoW[0,:,:]))
        # # Constraining the seam of the cylindrical patch
        # temp = 0.5*(inpCtrlPts.data[:,0,:] + inpCtrlPts.data[:,-1,:])
        # inpCtrlPts.data[:,0,:] = temp
        # inpCtrlPts.data[:,-1,:] = temp

        # for j in range(5):
        #     tempdir1 = inpCtrlPts.data[j][1] - inpCtrlPts.data[j][0]
        #     tempdir2 = inpCtrlPts.data[j][-1] - inpCtrlPts.data[j][-2]
        #     avgDir = 0.5*(tempdir1+tempdir2)
        #     inpCtrlPts.data[j][1] = inpCtrlPts.data[j][0] + avgDir
        #     inpCtrlPts.data[j][-2] = inpCtrlPts.data[j][0] - avgDir

        if i % 5000 == 0:
            fig = plt.figure(figsize=(4, 4))
            ax = fig.add_subplot(111, projection='3d', adjustable='box', proj_type='ortho')

            target_cpu = target.cpu().numpy().squeeze()
            predicted = out.detach().cpu().numpy().squeeze()
            predCtrlPts = inpCtrlPts.detach().cpu().numpy().squeeze()

            surf1 = ax.scatter(target_cpu[:, 0], target_cpu[:, 1], target_cpu[:, 2], s=3.0, color='red')
            surf2 = ax.plot_surface(predicted[:, :, 0], predicted[:, :, 1], predicted[:, :, 2], color='green', alpha=0.5)
            surf3 = ax.plot_wireframe(predCtrlPts[:, :, 0], predCtrlPts[:, :, 1], predCtrlPts[:, :, 2], linewidth=1, color='orange')
            #ax.plot(CtrlPtsNoW[0, :, 0], CtrlPtsNoW[0, :, 1], CtrlPtsNoW[0, :, 2], linewidth=3, linestyle='solid', color='green')

            ax.azim = -90
            ax.dist = 6.5
            ax.elev = 120
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_zticks([])
            # ax.set_box_aspect([0.1, 1, 0.1])
            ax.xaxis.set_pane_color((1.0, 1.0, 1.0, 0.0))
            ax.yaxis.set_pane_color((1.0, 1.0, 1.0, 0.0))
            ax.zaxis.set_pane_color((1.0, 1.0, 1.0, 0.0))
            ax._axis3don = False
            # ax.legend()

            # ax.set_aspect(1)
            fig.subplots_adjust(hspace=0, wspace=0)
            fig.tight_layout()
            plt.show()

        pbar.set_description("Total loss is %s: %s" % (i + 1, lossVal.item()))
        pass


    # predCtrlPts = torch.cat((inpCtrlPts.unsqueeze(0), weight), axis=-1).detach().cpu().numpy().squeeze()
    # predctrlptsw = (np.reshape(predCtrlPts,(CtrlPtsCountUV[0]*CtrlPtsCountUV[1], 4)).tolist())
    predCtrlPoints = inpCtrlPts.detach().cpu().numpy().squeeze().tolist()
    surface['shape']['data']['control_points']['points'] = predCtrlPoints

    with open(jsonOutFileName, "w") as f:
        json.dump(surface, f, indent=4)
    # surface.evaluate()
    # vis_config = VisMPL.VisConfig(legend=False, axes=False, ctrlpts=False)
    # vis_comp = VisMPL.VisSurface(vis_config)
    # surface.vis = vis_comp
    # surface.render()
    pass


if __name__ == '__main__':
    main()
