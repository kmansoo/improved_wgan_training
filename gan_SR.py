import os, sys
sys.path.append(os.getcwd())

import time
import functools

import numpy as np
import tensorflow as tf
import scipy.misc

import tflib as lib
import tflib.ops.linear
import tflib.ops.conv2d
import tflib.ops.batchnorm
import tflib.ops.deconv2d
import tflib.save_images
import tflib.celebA_64x64
import tflib.small_imagenet
import tflib.ops.layernorm
import tflib.plot

FLAGS = tf.app.flags.FLAGS

# Configurations
tf.app.flags.DEFINE_string('mode', 'wgan-gp',
                           "loss function option. [wgan-gp | dcgan | wgan | lsgan]")
tf.app.flags.DEFINE_string('data_dir', 'data/celebA_64x64', "data directory")
tf.app.flags.DEFINE_string('train_dir', 'train', "image output direcotory")
tf.app.flags.DEFINE_string('summary_dir', 'summary', "tensorboard summary directory")
tf.app.flags.DEFINE_integer('max_runtime', 20, "maximum run time in min")
tf.app.flags.DEFINE_integer('max_iter', 500, "maximum mini-batch iterations")
tf.app.flags.DEFINE_float('LAMBDA', 10., "gradient penalty lambda parameter")
tf.app.flags.DEFINE_float('gen_l1_weight', 0.9, "weight of L1 difference in generator loss")
tf.app.flags.DEFINE_integer('architecture', 0, "index of architecture")

# Download 64x64 ImageNet at http://image-net.org/small/download.php and
# fill in the path to the extracted files here!
DATA_DIR = FLAGS.data_dir
SUMMARY_DIR = FLAGS.summary_dir
GEN_L1_WEIGHT = FLAGS.gen_l1_weight # Weighting factor for L1 difference in generator loss
TRAIN_DIR = FLAGS.train_dir # Directory to output image
MODE = FLAGS.mode # dcgan, wgan, wgan-gp, lsgan
ITERS = FLAGS.max_iter # How many iterations to train for
LAMBDA = FLAGS.LAMBDA # Gradient penalty lambda hyperparameter

if len(DATA_DIR) == 0:
    raise Exception('Please specify path to data directory in gan_64x64.py!')

DIM = 64 # Model dimensionality
K = 4 # How much to downsample
CRITIC_ITERS = 5 # How many iterations to train the critic for
N_GPUS = 1 # Number of GPUs
BATCH_SIZE = 16 # Batch size. Must be a multiple of N_GPUS
INPUT_DIM = 16*16*3 # Number of pixels in each input
OUTPUT_DIM = 64*64*3 # Number of pixels in each iamge
DELETE_TRAIN_DIR=True

lib.print_model_settings(locals().copy())

# create summary dir
if not tf.gfile.Exists(FLAGS.summary_dir):
    tf.gfile.MakeDirs(FLAGS.summary_dir)

# clean directory
if DELETE_TRAIN_DIR:
    if tf.gfile.Exists(FLAGS.train_dir):
        tf.gfile.DeleteRecursively(FLAGS.train_dir)
        tf.gfile.MakeDirs(FLAGS.train_dir)
    tf.gfile.MakeDirs(FLAGS.train_dir)

# architecture dictionary
def get_architectures():
    ARCHITECTURE_TABLE = {
        # Baseline (G: DCGAN, D: DCGAN)
        0: (DCGANGenerator, DCGANDiscriminator),

        # No BN and constant number of filts in G
        1: (WGANPaper_CrippledDCGANGenerator, DCGANDiscriminator),

        # 512-dim 4-layer ReLU MLP G
        2: (FCGenerator, DCGANDiscriminator),

        # No normalization anywhere
        3: (functools.partial(DCGANGenerator, bn=False),
            functools.partial(DCGANDiscriminator, bn=False)),

        # Gated multiplicative nonlinearities everywhere
        4: (MultiplicativeDCGANGenerator, MultiplicativeDCGANDiscriminator),

        # tanh nonlinearities everywhere
        5: (functools.partial(DCGANGenerator, bn=True, nonlinearity=tf.tanh),
            functools.partial(DCGANDiscriminator, bn=True, nonlinearity=tf.tanh)),

        # 101-layer ResNet G and D
        6: (ResnetGenerator, ResnetDiscriminator)
    }
    return ARCHITECTURE_TABLE

def GeneratorAndDiscriminator():
    """
    Choose which generator and discriminator architecture to use by
    uncommenting one of these lines.
    """
    table = get_architectures()
    if FLAGS.architecture <= len(table):
        return table[FLAGS.architecture]

    raise Exception('You must choose an architecture!')

DEVICES = ['/gpu:{}'.format(i) for i in range(N_GPUS)]

def LeakyReLU(x, alpha=0.2):
    return tf.maximum(alpha*x, x)

def ReLULayer(name, n_in, n_out, inputs):
    output = lib.ops.linear.Linear(name+'.Linear', n_in, n_out, inputs, initialization='he')
    return tf.nn.relu(output)

def LeakyReLULayer(name, n_in, n_out, inputs):
    output = lib.ops.linear.Linear(name+'.Linear', n_in, n_out, inputs, initialization='he')
    return LeakyReLU(output)

def Batchnorm(name, axes, inputs):
    if ('Discriminator' in name) and (MODE == 'wgan-gp'):
        if axes != [0,2,3]:
            raise Exception('Layernorm over non-standard axes is unsupported')
        return lib.ops.layernorm.Layernorm(name,[1,2,3],inputs)
    else:
        return lib.ops.batchnorm.Batchnorm(name,axes,inputs,fused=True)

def pixcnn_gated_nonlinearity(a, b):
    return tf.sigmoid(a) * tf.tanh(b)

def SubpixelConv2D(*args, **kwargs):
    kwargs['output_dim'] = 4*kwargs['output_dim']
    output = lib.ops.conv2d.Conv2D(*args, **kwargs)
    output = tf.transpose(output, [0,2,3,1])
    output = tf.depth_to_space(output, 2)
    output = tf.transpose(output, [0,3,1,2])
    return output

def ResidualBlock(name, input_dim, output_dim, filter_size, inputs, resample=None, he_init=True):
    """
    resample: None, 'down', or 'up'
    """
    if resample=='down':
        conv_shortcut = functools.partial(lib.ops.conv2d.Conv2D, stride=2)
        conv_1        = functools.partial(lib.ops.conv2d.Conv2D, input_dim=input_dim, output_dim=input_dim//2)
        conv_1b       = functools.partial(lib.ops.conv2d.Conv2D, input_dim=input_dim//2, output_dim=output_dim//2, stride=2)
        conv_2        = functools.partial(lib.ops.conv2d.Conv2D, input_dim=output_dim//2, output_dim=output_dim)
    elif resample=='up':
        conv_shortcut = SubpixelConv2D
        conv_1        = functools.partial(lib.ops.conv2d.Conv2D, input_dim=input_dim, output_dim=input_dim//2)
        conv_1b       = functools.partial(lib.ops.deconv2d.Deconv2D, input_dim=input_dim//2, output_dim=output_dim//2)
        conv_2        = functools.partial(lib.ops.conv2d.Conv2D, input_dim=output_dim//2, output_dim=output_dim)
    elif resample==None:
        conv_shortcut = lib.ops.conv2d.Conv2D
        conv_1        = functools.partial(lib.ops.conv2d.Conv2D, input_dim=input_dim,  output_dim=input_dim//2)
        conv_1b       = functools.partial(lib.ops.conv2d.Conv2D, input_dim=input_dim//2,  output_dim=output_dim/2)
        conv_2        = functools.partial(lib.ops.conv2d.Conv2D, input_dim=input_dim//2, output_dim=output_dim)

    else:
        raise Exception('invalid resample value')

    if output_dim==input_dim and resample==None:
        shortcut = inputs # Identity skip-connection
    else:
        shortcut = conv_shortcut(name+'.Shortcut', input_dim=input_dim, output_dim=output_dim, filter_size=1,
                                 he_init=False, biases=True, inputs=inputs)

    output = inputs
    output = tf.nn.relu(output)
    output = conv_1(name+'.Conv1', filter_size=1, inputs=output, he_init=he_init, weightnorm=False)
    output = tf.nn.relu(output)
    output = conv_1b(name+'.Conv1B', filter_size=filter_size, inputs=output, he_init=he_init, weightnorm=False)
    output = tf.nn.relu(output)
    output = conv_2(name+'.Conv2', filter_size=1, inputs=output, he_init=he_init, weightnorm=False, biases=False)
    output = Batchnorm(name+'.BN', [0,2,3], output)

    return shortcut + (0.3*output)

# ! Generators

def FCGenerator(n_samples, noise=None, FC_DIM=512, input_dim=INPUT_DIM):
    if noise is None:
        noise = tf.random_normal([n_samples, input_dim])

    output = ReLULayer('Generator.1', input_dim, FC_DIM, noise)
    output = ReLULayer('Generator.2', FC_DIM, FC_DIM, output)
    output = ReLULayer('Generator.3', FC_DIM, FC_DIM, output)
    output = ReLULayer('Generator.4', FC_DIM, FC_DIM, output)
    output = lib.ops.linear.Linear('Generator.Out', FC_DIM, OUTPUT_DIM, output)

    output = tf.tanh(output)

    return output

def DCGANGenerator(
        n_samples, noise=None, dim=DIM, input_dim=INPUT_DIM,
        k=K, bn=True, nonlinearity=tf.nn.relu):

    lib.ops.conv2d.set_weights_stdev(0.02)
    lib.ops.deconv2d.set_weights_stdev(0.02)
    lib.ops.linear.set_weights_stdev(0.02)
    
    if noise is None:
        noise = tf.random_normal([n_samples, input_dim])
        output = lib.ops.linear.Linear(
            'Generator.Input', 256, (dim//k)*(dim//k)*8*dim, noise)
        output = tf.reshape(output, [-1, 8*dim, dim//k, dim//k])
        if bn:
            output = Batchnorm('Generator.BN1', [0,2,3], output)
            output = nonlinearity(output)
    else:
        # downsampled data as input (noise)
        # input (noise) dimension [batchsize, 3*(dim/K)*(dim/K)]
        # decode twice to tensor of [batchsize, 8*dim, 4, 4]
        output = tf.reshape(noise, [-1, 3, dim//k, dim//k])
        output = tflib.ops.conv2d.Conv2D(
            'Generator.Encoder1.1', 3, 4*dim, 5, output, stride=2)
        if bn:
            output = Batchnorm('Generator.BN1.1', [0,2,3], output)
            output = nonlinearity(output)
            
        output = tflib.ops.conv2d.Conv2D(
            'Generator.Encode1.2', 4*dim, 8*dim, 5, output, stride=2)
        if bn:
            output = Batchnorm('Generator.BN1.2', [0, 2, 3], output)
            output = nonlinearity(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.2', 8*dim, 4*dim, 5, output)
    if bn:
        output = Batchnorm('Generator.BN2', [0,2,3], output)
    output = nonlinearity(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.3', 4*dim, 2*dim, 5, output)
    if bn:
        output = Batchnorm('Generator.BN3', [0,2,3], output)
    output = nonlinearity(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.4', 2*dim, dim, 5, output)
    if bn:
        output = Batchnorm('Generator.BN4', [0,2,3], output)
    output = nonlinearity(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.5', dim, 3, 5, output)
    output = tf.tanh(output)
    
    lib.ops.conv2d.unset_weights_stdev()
    lib.ops.deconv2d.unset_weights_stdev()
    lib.ops.linear.unset_weights_stdev()

    return tf.reshape(output, [-1, OUTPUT_DIM])

def WGANPaper_CrippledDCGANGenerator(
        n_samples, noise=None, dim=DIM, input_dim=INPUT_DIM):
    if noise is None:
        noise = tf.random_normal([n_samples, input_dim])

    output = lib.ops.linear.Linear('Generator.Input', input_dim, 4*4*dim, noise)
    output = tf.nn.relu(output)
    output = tf.reshape(output, [-1, dim, 4, 4])

    output = lib.ops.deconv2d.Deconv2D('Generator.2', dim, dim, 5, output)
    output = tf.nn.relu(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.3', dim, dim, 5, output)
    output = tf.nn.relu(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.4', dim, dim, 5, output)
    output = tf.nn.relu(output)

    output = lib.ops.deconv2d.Deconv2D('Generator.5', dim, 3, 5, output)
    output = tf.tanh(output)

    return tf.reshape(output, [-1, OUTPUT_DIM])

def ResnetGenerator(n_samples, noise=None, dim=DIM, input_dim=INPUT_DIM):
    if noise is None:
        noise = tf.random_normal([n_samples, input_dim])

    output = lib.ops.linear.Linear('Generator.Input', input_dim, 4*4*8*dim, noise)
    output = tf.reshape(output, [-1, 8*dim, 4, 4])

    for i in range(6):
        output = ResidualBlock('Generator.4x4_{}'.format(i), 8*dim, 8*dim, 3, output, resample=None)
    output = ResidualBlock('Generator.Up1', 8*dim, 4*dim, 3, output, resample='up')
    for i in range(6):
        output = ResidualBlock('Generator.8x8_{}'.format(i), 4*dim, 4*dim, 3, output, resample=None)
    output = ResidualBlock('Generator.Up2', 4*dim, 2*dim, 3, output, resample='up')
    for i in range(6):
        output = ResidualBlock('Generator.16x16_{}'.format(i), 2*dim, 2*dim, 3, output, resample=None)
    output = ResidualBlock('Generator.Up3', 2*dim, 1*dim, 3, output, resample='up')
    for i in range(6):
        output = ResidualBlock('Generator.32x32_{}'.format(i), 1*dim, 1*dim, 3, output, resample=None)
    output = ResidualBlock('Generator.Up4', 1*dim, dim//2, 3, output, resample='up')
    for i in range(5):
        output = ResidualBlock('Generator.64x64_{}'.format(i), dim/2, dim/2, 3, output, resample=None)

    output = lib.ops.conv2d.Conv2D('Generator.Out', dim//2, 3, 1, output, he_init=False)
    output = tf.tanh(output / 5.)

    return tf.reshape(output, [-1, OUTPUT_DIM])


def MultiplicativeDCGANGenerator(n_samples, noise=None, dim=DIM, bn=True, input_dim=INPUT_DIM):
    if noise is None:
        noise = tf.random_normal([n_samples, input_dim])

    output = lib.ops.linear.Linear('Generator.Input', input_dim, 4*4*8*dim*2, noise)
    output = tf.reshape(output, [-1, 8*dim*2, 4, 4])
    if bn:
        output = Batchnorm('Generator.BN1', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.deconv2d.Deconv2D('Generator.2', 8*dim, 4*dim*2, 5, output)
    if bn:
        output = Batchnorm('Generator.BN2', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.deconv2d.Deconv2D('Generator.3', 4*dim, 2*dim*2, 5, output)
    if bn:
        output = Batchnorm('Generator.BN3', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.deconv2d.Deconv2D('Generator.4', 2*dim, dim*2, 5, output)
    if bn:
        output = Batchnorm('Generator.BN4', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.deconv2d.Deconv2D('Generator.5', dim, 3, 5, output)
    output = tf.tanh(output)

    return tf.reshape(output, [-1, OUTPUT_DIM])

# ! Discriminators

def MultiplicativeDCGANDiscriminator(inputs, dim=DIM, bn=True):
    output = tf.reshape(inputs, [-1, 3, 64, 64])

    output = lib.ops.conv2d.Conv2D('Discriminator.1', 3, dim*2, 5, output, stride=2)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.conv2d.Conv2D('Discriminator.2', dim, 2*dim*2, 5, output, stride=2)
    if bn:
        output = Batchnorm('Discriminator.BN2', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.conv2d.Conv2D('Discriminator.3', 2*dim, 4*dim*2, 5, output, stride=2)
    if bn:
        output = Batchnorm('Discriminator.BN3', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = lib.ops.conv2d.Conv2D('Discriminator.4', 4*dim, 8*dim*2, 5, output, stride=2)
    if bn:
        output = Batchnorm('Discriminator.BN4', [0,2,3], output)
    output = pixcnn_gated_nonlinearity(output[:,::2], output[:,1::2])

    output = tf.reshape(output, [-1, 4*4*8*dim])
    output = lib.ops.linear.Linear('Discriminator.Output', 4*4*8*dim, 1, output)

    return tf.reshape(output, [-1])


def ResnetDiscriminator(inputs, dim=DIM):
    output = tf.reshape(inputs, [-1, 3, 64, 64])
    output = lib.ops.conv2d.Conv2D('Discriminator.In', 3, dim//2, 1, output, he_init=False)

    for i in range(5):
        output = ResidualBlock('Discriminator.64x64_{}'.format(i), dim/2, dim/2, 3, output, resample=None)
    output = ResidualBlock('Discriminator.Down1', dim//2, dim*1, 3, output, resample='down')
    for i in range(6):
        output = ResidualBlock('Discriminator.32x32_{}'.format(i), dim*1, dim*1, 3, output, resample=None)
    output = ResidualBlock('Discriminator.Down2', dim*1, dim*2, 3, output, resample='down')
    for i in range(6):
        output = ResidualBlock('Discriminator.16x16_{}'.format(i), dim*2, dim*2, 3, output, resample=None)
    output = ResidualBlock('Discriminator.Down3', dim*2, dim*4, 3, output, resample='down')
    for i in range(6):
        output = ResidualBlock('Discriminator.8x8_{}'.format(i), dim*4, dim*4, 3, output, resample=None)
    output = ResidualBlock('Discriminator.Down4', dim*4, dim*8, 3, output, resample='down')
    for i in range(6):
        output = ResidualBlock('Discriminator.4x4_{}'.format(i), dim*8, dim*8, 3, output, resample=None)

    output = tf.reshape(output, [-1, 4*4*8*dim])
    output = lib.ops.linear.Linear('Discriminator.Output', 4*4*8*dim, 1, output)

    return tf.reshape(output / 5., [-1])


def FCDiscriminator(inputs, FC_DIM=512, n_layers=3):
    output = LeakyReLULayer('Discriminator.Input', OUTPUT_DIM, FC_DIM, inputs)
    for i in range(n_layers):
        output = LeakyReLULayer('Discriminator.{}'.format(i), FC_DIM, FC_DIM, output)
    output = lib.ops.linear.Linear('Discriminator.Out', FC_DIM, 1, output)

    return tf.reshape(output, [-1])

def DCGANDiscriminator(inputs, dim=DIM, bn=True, nonlinearity=LeakyReLU):
    output = tf.reshape(inputs, [-1, 3, 64, 64])

    lib.ops.conv2d.set_weights_stdev(0.02)
    lib.ops.deconv2d.set_weights_stdev(0.02)
    lib.ops.linear.set_weights_stdev(0.02)

    output = lib.ops.conv2d.Conv2D('Discriminator.1', 3, dim, 5, output, stride=2)
    output = nonlinearity(output)

    output = lib.ops.conv2d.Conv2D('Discriminator.2', dim, 2*dim, 5, output, stride=2)
    if bn:
        output = Batchnorm('Discriminator.BN2', [0,2,3], output)
    output = nonlinearity(output)

    output = lib.ops.conv2d.Conv2D('Discriminator.3', 2*dim, 4*dim, 5, output, stride=2)
    if bn:
        output = Batchnorm('Discriminator.BN3', [0,2,3], output)
    output = nonlinearity(output)

    output = lib.ops.conv2d.Conv2D('Discriminator.4', 4*dim, 8*dim, 5, output, stride=2)
    if bn:
        output = Batchnorm('Discriminator.BN4', [0,2,3], output)
    output = nonlinearity(output)

    output = tf.reshape(output, [-1, 4*4*8*dim])
    output = lib.ops.linear.Linear('Discriminator.Output', 4*4*8*dim, 1, output)

    lib.ops.conv2d.unset_weights_stdev()
    lib.ops.deconv2d.unset_weights_stdev()
    lib.ops.linear.unset_weights_stdev()

    return tf.reshape(output, [-1])

# kernel for downsampling
arr = np.zeros([K, K, 3, 3])
arr[:,:,0,0] = 1.0/(K*K)
arr[:,:,1,1] = 1.0/(K*K)
arr[:,:,2,2] = 1.0/(K*K)
_downsample_weight = tf.constant(arr, dtype=tf.float32)

def downsample(data, method='conv'):
    data = tf.reshape(data, [-1, 3, DIM, DIM])
    # BCHW -> BHWC
    data = tf.transpose(data, [0, 2, 3, 1])
    if method == 'conv':
        data = tf.nn.conv2d(data, _downsample_weight,
                            strides=[1, K, K, 1], padding='SAME')
    elif method == 'area':
        data = tf.image.resize_area(data, [DIM//K, DIM//K])
    # BHWC -> BCHW
    data = tf.transpose(data, [0, 3, 1, 2])
    data = tf.reshape(data, [-1, 3 * DIM//K * DIM//K])
    return data

Generator, Discriminator = GeneratorAndDiscriminator()

with tf.Session(config=tf.ConfigProto(allow_soft_placement=True)) as session:

    all_real_data_conv = tf.placeholder(tf.int32, shape=[BATCH_SIZE, 3, 64, 64])
    if tf.__version__.startswith('1.'):
        split_real_data_conv = tf.split(all_real_data_conv, len(DEVICES))
    else:
        split_real_data_conv = tf.split(0, len(DEVICES), all_real_data_conv)

    gen_l1_costs, gen_gan_costs = [], []
    gen_costs, disc_costs = [],[]

    for device_index, (device, real_data_conv) in enumerate(zip(DEVICES, split_real_data_conv)):
        with tf.device(device):
            real_data = 2*((tf.cast(real_data_conv, tf.float32)/255.)-.5)
            real_data = tf.reshape(real_data, [BATCH_SIZE//len(DEVICES), OUTPUT_DIM])
            # downsampled (by K) as generator input
            real_data_downsampled = downsample(real_data)
            fake_data = Generator(BATCH_SIZE//len(DEVICES), noise=real_data_downsampled)
            
            disc_real = Discriminator(real_data)
            disc_fake = Discriminator(fake_data)

            if MODE == 'wgan':
                gen_cost = tf.reduce_mean(disc_fake)
                disc_cost = tf.reduce_mean(disc_real) - tf.reduce_mean(disc_fake)

            elif MODE == 'wgan-gp':
                gen_cost = tf.reduce_mean(disc_fake)
                disc_cost = tf.reduce_mean(disc_real) - tf.reduce_mean(disc_fake)

                alpha = tf.random_uniform(
                    shape=[BATCH_SIZE//len(DEVICES),1], 
                    minval=0.,
                    maxval=1.
                )
                differences = fake_data - real_data
                interpolates = real_data + (alpha*differences)
                gradients = tf.gradients(Discriminator(interpolates), [interpolates])[0]
                slopes = tf.sqrt(tf.reduce_sum(tf.square(gradients), reduction_indices=[1]))
                gradient_penalty = tf.reduce_mean((slopes-1.)**2)
                disc_cost += LAMBDA*gradient_penalty

            elif MODE == 'dcgan':
                try: # tf pre-1.0 (bottom) vs 1.0 (top)
                    gen_cost = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=disc_fake,
                                                                                      labels=tf.ones_like(disc_fake)))
                    disc_cost =  tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=disc_fake,
                                                                                        labels=tf.zeros_like(disc_fake)))
                    disc_cost += tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=disc_real,
                                                                                        labels=tf.ones_like(disc_real)))                    
                except Exception as e:
                    gen_cost = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_fake, tf.ones_like(disc_fake)))
                    disc_cost =  tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_fake, tf.zeros_like(disc_fake)))
                    disc_cost += tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(disc_real, tf.ones_like(disc_real)))                    
                disc_cost /= 2.

            elif MODE == 'lsgan':
                gen_cost = tf.reduce_mean((disc_fake - 1)**2)
                disc_cost = (tf.reduce_mean((disc_real - 1)**2) + tf.reduce_mean((disc_fake - 0)**2))/2.

            else:
                raise Exception()

            # add L1 difference to penalty
            fake_data_downsampled = downsample(fake_data)
            gen_l1_cost = tf.reduce_mean(
                tf.abs(fake_data_downsampled - real_data_downsampled))

            gen_l1_costs.append(gen_l1_cost)
            gen_gan_costs.append(gen_cost)

            gen_cost = GEN_L1_WEIGHT * gen_l1_cost + (1 - GEN_L1_WEIGHT) * gen_cost

            gen_costs.append(gen_cost)
            disc_costs.append(disc_cost)

    gen_cost = tf.add_n(gen_costs) / len(DEVICES)
    disc_cost = tf.add_n(disc_costs) / len(DEVICES)
    gen_gan_cost = tf.add_n(gen_gan_costs) / len(DEVICES)
    gen_l1_cost = tf.add_n(gen_l1_costs) / len(DEVICES)
    tf.summary.scalar('gen gan loss', gen_gan_cost, collections=['scalars'])
    tf.summary.scalar('gen l1 diff', gen_l1_cost, collections=['scalars'])
    tf.summary.scalar('gen loss', gen_cost, collections=['scalars'])
    tf.summary.scalar('disc loss', disc_cost, collections=['scalars'])

    if MODE == 'wgan':
        gen_train_op = tf.train.RMSPropOptimizer(learning_rate=1e-4).minimize(
            gen_cost, var_list=lib.params_with_name('Generator'), colocate_gradients_with_ops=True)
        disc_train_op = tf.train.RMSPropOptimizer(learning_rate=1e-4).minimize(disc_cost,
                                             var_list=lib.params_with_name('Discriminator.'), colocate_gradients_with_ops=True)

        clip_ops = []
        for var in lib.params_with_name('Discriminator'):
            clip_bounds = [-.01, .01]
            clip_ops.append(tf.assign(var, tf.clip_by_value(var, clip_bounds[0], clip_bounds[1])))
        clip_disc_weights = tf.group(*clip_ops)

    elif MODE == 'wgan-gp':
        gen_train_op = tf.train.AdamOptimizer(
            learning_rate=1e-4, beta1=0.5, beta2=0.9).minimize(
                gen_cost,var_list=lib.params_with_name('Generator'), colocate_gradients_with_ops=True)
        disc_train_op = tf.train.AdamOptimizer(learning_rate=1e-4, beta1=0.5, beta2=0.9).minimize(disc_cost,
                                           var_list=lib.params_with_name('Discriminator.'), colocate_gradients_with_ops=True)

    elif MODE == 'dcgan':
        gen_train_op = tf.train.AdamOptimizer(learning_rate=2e-4, beta1=0.5).minimize(gen_cost,
                                          var_list=lib.params_with_name('Generator'), colocate_gradients_with_ops=True)
        disc_train_op = tf.train.AdamOptimizer(learning_rate=2e-4, beta1=0.5).minimize(disc_cost,
                                           var_list=lib.params_with_name('Discriminator.'), colocate_gradients_with_ops=True)

    elif MODE == 'lsgan':
        gen_train_op = tf.train.RMSPropOptimizer(learning_rate=1e-4).minimize(gen_cost,
                                             var_list=lib.params_with_name('Generator'), colocate_gradients_with_ops=True)
        disc_train_op = tf.train.RMSPropOptimizer(learning_rate=1e-4).minimize(disc_cost,
                                              var_list=lib.params_with_name('Discriminator.'), colocate_gradients_with_ops=True)

    else:
        raise Exception()

#     # For generating samples
#     fixed_noise = tf.constant(np.random.normal(size=(BATCH_SIZE, INPUT_DIM)).astype('float32'))
#     all_fixed_noise_samples = []
#     for device_index, device in enumerate(DEVICES):
#         n_samples = BATCH_SIZE // len(DEVICES)
#         all_fixed_noise_samples.append(Generator(n_samples, noise=fixed_noise[device_index*n_samples:(device_index+1)*n_samples]))
#     if tf.__version__.startswith('1.'):
#         all_fixed_noise_samples = tf.concat(all_fixed_noise_samples, axis=0)
#     else:
#         all_fixed_noise_samples = tf.concat(0, all_fixed_noise_samples)

#     def generate_image(iteration):
#         # add image to summary
#         samples_reshaped = tf.reshape(
#             all_fixed_noise_samples, (BATCH_SIZE, 3, DIM, DIM))
#         samples_reshaped = tf.transpose(samples_reshaped, [0, 2, 3, 1])
#         image_op = tf.summary.image(
#             'generator output', samples_reshaped)
#         image_summary = session.run(image_op)
#         summary_writer.add_summary(image_summary, iteration)

#         samples = session.run(all_fixed_noise_samples)
#         samples = ((samples+1.)*(255.99/2)).astype('int32')
#         lib.save_images.save_images(samples.reshape((BATCH_SIZE, 3, 64, 64)), 'samples_{}.png'.format(iteration))

    
    def generate_test_image(iteration, real_data, fake_data,  max_samples=10):
        feature = tf.reshape(real_data_downsampled, [-1, 3, DIM//K, DIM//K])
        # BCHW -> BHWC
        feature = (tf.transpose(feature, [0, 2, 3, 1]) + 1)/2.
        nearest = tf.image.resize_nearest_neighbor(feature, [DIM, DIM])
        nearest = tf.maximum(tf.minimum(nearest, 1.), 0.)
        bicubic = tf.image.resize_bicubic(feature, [DIM, DIM])
        bicubic = tf.maximum(tf.minimum(bicubic, 1.), 0.)
        fake_data = (tf.reshape(fake_data, [-1, 3, DIM, DIM]) + 1.)/2.
        fake_data = tf.transpose(fake_data, [0, 2, 3, 1])
        real_data = tf.reshape(real_data, [-1, 3, DIM, DIM])
        real_data = tf.transpose(real_data, [0, 2, 3, 1])
        real_data = (real_data + 1.) / 2.
        clipped = tf.maximum(tf.minimum(fake_data, 1.), 0.)
        image = tf.concat([nearest, bicubic, clipped, real_data], 2)

        feed_dict = {real_data_conv: test_data}
        image_col = tf.summary.image('generator output', image, max_samples)
        image_summary = session.run(image_col, feed_dict=feed_dict)
        summary_writer.add_summary(image_summary, iteration)

        image = image[0:max_samples,:,:,:]
        image = tf.concat([image[i,:,:,:] for i in range(max_samples)], 0)
        clipped = clipped[0:max_samples, :, :, :]
        clipped = tf.concat([clipped[i, :, :, :] for i in range(max_samples)], 1)

        image, clipped = session.run([image, clipped], feed_dict=feed_dict)
        
        filename_1 = 'batch%06d_image.png' % iteration
        filename_2 = 'batch%06d_row.png' % iteration
        filename_1 = os.path.join(TRAIN_DIR, filename_1)
        filename_2 = os.path.join(TRAIN_DIR, filename_2)
        scipy.misc.toimage(image, cmin=0., cmax=1.).save(filename_1)
        scipy.misc.toimage(clipped, cmin=0., cmax=1.).save(filename_2)
        print("Saved %s %s" % (filename_1, filename_2))

        



    # Dataset iterator and test set (for visualization) 
    train_gen, test_data = lib.celebA_64x64.load(BATCH_SIZE, data_dir=DATA_DIR)
    #train_gen, dev_gen = lib.small_imagenet.load(BATCH_SIZE, data_dir=DATA_DIR)

    def inf_train_gen():
        while True:
            for (images,) in train_gen():
                yield images

    # Save a batch of ground-truth samples
    _x = next(inf_train_gen())
    _x_r = session.run(real_data, feed_dict={real_data_conv: _x})
    _x_r = ((_x_r+1.)*(255.99/2)).astype('int32')
    lib.save_images.save_images(_x_r.reshape((BATCH_SIZE, 3, 64, 64)), 'samples_groundtruth.png')

    # Train loop
    merged_scalars = tf.summary.merge_all(key='scalars')
    summary_writer = tf.summary.FileWriter(SUMMARY_DIR, session.graph)

    session.run(tf.global_variables_initializer())
    gen = inf_train_gen()
    all_start_time = time.time()
    for iteration in range(ITERS):
        start_time = time.time()
        # finish if run overtime
        total_elapsed = (start_time - all_start_time) / 60.
        if total_elapsed > FLAGS.max_runtime:
            break

        # Train generator
        if iteration > 0:
            _ = session.run(gen_train_op, feed_dict={all_real_data_conv: _data})

        # Train critic
        if (MODE == 'dcgan') or (MODE == 'lsgan'):
            disc_iters = 1
        else:
            disc_iters = CRITIC_ITERS
        for i in range(disc_iters):
            _data = next(gen)
            _disc_cost, _ = session.run([disc_cost, disc_train_op], feed_dict={all_real_data_conv: _data})
            if MODE == 'wgan':
                _ = session.run([clip_disc_weights])

        lib.plot.plot('train disc cost', _disc_cost)
        lib.plot.plot('time', time.time() - start_time)
        #print('iter={0} disc_loss={1:.3g} time={2:.2g}'.format(
        #    iteration, _disc_cost, time.time() - start_time))

        if iteration % 10 == 0:
            merged_summary = session.run(merged_scalars, feed_dict={all_real_data_conv: _data})
            summary_writer.add_summary(merged_summary, iteration)

        if iteration % 200 == 9:
            t = time.time()
            #dev_disc_costs = []
            #for (images,) in dev_gen():
            #    _dev_disc_cost = session.run(disc_cost, feed_dict={all_real_data_conv: _data}) 
            #    dev_disc_costs.append(_dev_disc_cost)
            #lib.plot.plot('dev disc cost', np.mean(dev_disc_costs))
            generate_test_image(iteration, real_data, fake_data)

        if (iteration < 5) or (iteration % 200 == 199):
            lib.plot.flush()

        lib.plot.tick()


if __name__ == '__main__':
    tf.app.run()
