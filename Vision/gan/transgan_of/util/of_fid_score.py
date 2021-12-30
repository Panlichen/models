import oneflow as flow
import numpy as np
from tqdm import tqdm
import os
from util.inception import InceptionV3

# A pytorch implementation of cov, from Modar M. Alfadly
# https://discuss.pytorch.org/t/covariance-and-gradient-support/16217/2
def flow_cov(m, rowvar=False):
    '''Estimate a covariance matrix given data.
    Covariance indicates the level to which two variables vary together.
    If we examine N-dimensional samples, `X = [x_1, x_2, ... x_N]^T`,
    then the covariance matrix element `C_{ij}` is the covariance of
    `x_i` and `x_j`. The element `C_{ii}` is the variance of `x_i`.
    Args:
        m: A 1-D or 2-D array containing multiple variables and observations.
            Each row of `m` represents a variable, and each column a single
            observation of all those variables.
        rowvar: If `rowvar` is True, then each row represents a
            variable, with observations in the columns. Otherwise, the
            relationship is transposed: each column represents a variable,
            while the rows contain observations.
    Returns:
        The covariance matrix of the variables.
    '''
    if m.dim() > 2:
        raise ValueError('m has more than 2 dimensions')
    if m.dim() < 2:
        m = m.view(1, -1)
    if not rowvar and m.size(0) != 1:
        m = m.transpose(-1, -2)
    # m = m.type(torch.double)  # uncomment this line if desired
    fact = 1.0 / (m.size(1) - 1)
    m -= flow.mean(m, dim=1, keepdim=True)
    mt = m.transpose(-1, -2)  # if complex: mt = m.t().conj()
    return fact * m.matmul(mt).squeeze()

# Pytorch implementation of matrix sqrt, from Tsung-Yu Lin, and Subhransu Maji
# https://github.com/msubhransu/matrix-sqrt
def sqrt_newton_schulz(A, numIters, dtype=None):
    if dtype is None:
        dtype = A.dtype
    batchSize = A.shape[0]
    dim = A.shape[1]
    normA = A.mul(A).sum(dim=1).sum(dim=1).sqrt()
    Y = A.div(normA.view(batchSize, 1, 1).expand_as(A)).to("cuda:0")
    I = flow.eye(dim, dim, dtype=dtype).view(1, dim, dim).repeat(batchSize, 1, 1).to("cuda:0")
    Z = flow.eye(dim, dim, dtype=dtype).view(1, dim, dim).repeat(batchSize, 1, 1).to("cuda:0")
    for i in range(numIters):
        T = 0.5 * (3.0 * I - Z.bmm(Y))
        Y = Y.bmm(T)
        Z = T.bmm(Z)
    sA = Y * flow.sqrt(normA).view(batchSize, 1, 1).expand_as(A)
    return sA


def get_activations(args, gen_net, model, batch_size=50, dims=2048,
                    cuda=False, verbose=False):
    """Calculates the activations of the pool_3 layer for all images.
    Params:
    -- files       : List of image files paths
    -- model       : Instance of inception model
    -- batch_size  : Batch size of images for the model to process at once.
                     Make sure that the number of samples is a multiple of
                     the batch size, otherwise some samples are ignored. This
                     behavior is retained to match the original FID score
                     implementation.
    -- dims        : Dimensionality of features returned by Inception
    -- cuda        : If set to True, use GPU
    -- verbose     : If set to True and parameter out_step is given, the number
                     of calculated batches is reported.
    Returns:
    -- A numpy array of dimension (num images, dims) that contains the
       activations of the given tensor when feeding inception with the
       query tensor.
    """
    with flow.no_grad():
        gen_net.eval()
        model.eval()
        n_batches = args.num_eval_imgs // batch_size

        # normalize
        pred_arr = []
        for i in tqdm(range(n_batches)):
            z = flow.tensor(np.random.normal(0, 1, (batch_size, args.latent_dim)), dtype=flow.float32).cuda()
            gen_imgs = gen_net(z, 200)
            
            if verbose:
                print('\rPropagating batch %d/%d' % (i + 1, n_batches),
                      end='', flush=True)
            start = i * batch_size
            end = start + batch_size

            images = (gen_imgs + 1.0) / 2.0
            model.to("cuda:0")
            pred = model(images.to("cuda:0"))[0]

            # If model output is not scalar, apply global spatial average pooling.
            # This happens if you choose a dimensionality not equal 2048.
            if pred.shape[2] != 1 or pred.shape[3] != 1:
                pred = flow.nn.functional.adaptive_avg_pool2d(pred, output_size=(1, 1))

            pred_arr += [pred.view(batch_size, -1)]
        if verbose:
            print('done')
        del images

    return flow.cat(pred_arr, dim=0)


def torch_calculate_frechet_distance(mu1, sigma1, mu2, sigma2, eps=1e-6):
    """Pytorch implementation of the Frechet Distance.
    Taken from https://github.com/bioinf-jku/TTUR
    The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
    and X_2 ~ N(mu_2, C_2) is
            d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).
    Stable version by Dougal J. Sutherland.
    Params:
    -- mu1   : Numpy array containing the activations of a layer of the
               inception net (like returned by the function 'get_predictions')
               for generated samples.
    -- mu2   : The sample mean over activations, precalculated on an
               representive data set.
    -- sigma1: The covariance matrix over activations for generated samples.
    -- sigma2: The covariance matrix over activations, precalculated on an
               representive data set.
    Returns:
    --   : The Frechet Distance.
    """

    assert mu1.shape == mu2.shape, \
        'Training and test mean vectors have different lengths'
    assert sigma1.shape == sigma2.shape, \
        'Training and test covariances have different dimensions'

    diff = mu1 - mu2
    # Run 50 itrs of newton-schulz to get the matrix sqrt of sigma1 dot sigma2
    covmean = sqrt_newton_schulz(flow.matmul(sigma1, sigma2).unsqueeze(0), 50).squeeze()
    out = ((diff*diff).sum() + flow.tensor(np.trace(sigma1.numpy())).to("cuda:0") + flow.tensor(np.trace(sigma2.numpy())).to("cuda:0")
           - 2 * flow.tensor(np.trace(covmean.numpy())).to("cuda:0"))
    return out


def calculate_activation_statistics(gen_net, model, batch_size=50,
                                    dims=2048, cuda=False, verbose=False):
    """Calculation of the statistics used by the FID.
    Params:
    -- gen_imgs    : gen_imgs, tensor
    -- model       : Instance of inception model
    -- batch_size  : The images numpy array is split into batches with
                     batch size batch_size. A reasonable batch size
                     depends on the hardware.
    -- dims        : Dimensionality of features returned by Inception
    -- cuda        : If set to True, use GPU
    -- verbose     : If set to True and parameter out_step is given, the
                     number of calculated batches is reported.
    Returns:
    -- mu    : The mean over samples of the activations of the pool_3 layer of
               the inception model.
    -- sigma : The covariance matrix of the activations of the pool_3 layer of
               the inception model.
    """
    act = get_activations(gen_net, model, batch_size, dims, cuda, verbose)
    mu = flow.mean(act, dim=0)
    sigma = flow_cov(act, rowvar=False)
    return mu, sigma


def _compute_statistics_of_path(args, path, model, batch_size, dims, cuda):
    if isinstance(path, str):
        assert path.endswith('.npz')
        f = np.load(path)
        if 'mean' in f:
            m, s = f['mean'][:], f['cov'][:]
        else:
            m, s = f['mu'][:], f['sigma'][:]
        f.close()
    else:
        # a tensor
        gen_net = path
        m, s = calculate_activation_statistics(args, gen_net, model, batch_size, dims, cuda)
    return m, s


def calculate_fid_given_paths_torch(args, gen_net, path, require_grad=False, gen_batch_size=1, batch_size=1, cuda=True, dims=2048):
    """
    Calculates the FID of two paths
    :param gen_imgs: The value range of gen_imgs should be (-1, 1). Just the output of tanh.
    :param path: fid file path. *.npz.
    :param batch_size:
    :param cuda:
    :param dims:
    :return:
    """
    if not os.path.exists(path):
        raise RuntimeError('Invalid path: %s' % path)

    assert args.num_eval_imgs >= dims, f'gen_imgs size: {args.num_eval_imgs}'  # or will lead to nan

    with flow.no_grad():
        block_idx = InceptionV3.BLOCK_INDEX_BY_DIM[dims]
        model = InceptionV3([block_idx])
        if cuda:
            model.cuda()

        m1, s1 = _compute_statistics_of_path(args, gen_net, model, batch_size,
                                             dims, cuda)
        # print(f'generated stat: {m1}, {s1}')
        m2, s2 = _compute_statistics_of_path(args, path, model, batch_size,
                                             dims, cuda)
        # print(f'GT stat: {m2}, {s2}')
        fid_value = torch_calculate_frechet_distance(m1.to("cuda:0"), s1.to("cuda:0"), flow.tensor(m2).float().cuda().to("cuda:0"),
                                                     flow.tensor(s2).float().cuda().to("cuda:0"))
        del model

    return fid_value



def get_fid(args, fid_stat, epoch, gen_net, num_img, gen_batch_size, val_batch_size, writer_dict=None, cls_idx=None):
    gen_net.eval()
    with flow.no_grad():
        # eval mode
        gen_net.eval()
        fid_score = calculate_fid_given_paths_torch(args, gen_net, fid_stat, gen_batch_size=gen_batch_size, batch_size=val_batch_size)

    if writer_dict:
        writer = writer_dict['writer']
        global_steps = writer_dict['valid_global_steps']
        writer.add_scalar('FID_score', fid_score.numpy(), global_steps)
        writer_dict['valid_global_steps'] = global_steps + 1

    return fid_score