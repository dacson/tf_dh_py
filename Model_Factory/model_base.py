# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Builds the calusa_heatmap network.

Summary of available functions:

 # Compute input images and labels for training. If you would like to run
 # evaluations, use inputs() instead.
 inputs, labels = inputs()

 # Compute inference on the model inputs to make a prediction.
 predictions = inference(inputs)

 # Compute the total loss of the prediction with respect to the labels.
 loss = loss(predictions, labels)

 # Create a graph to run one step of training with respect to the loss.
 train_op = train(loss, global_step)
"""
# pylint: disable=missing-docstring
from __future__ import absolute_import
from __future__ import division

import re

import tensorflow as tf
import numpy as np

import Model_Factory.optimizer_params as optimizer_params
import Model_Factory.loss_base as loss_base

# If a model is trained with multiple GPUs, prefix all Op names with tower_name
# to differentiate the operations. Note that this prefix is removed from the
# names of the summaries when visualizing a model.
TOWER_NAME = 'tower'

def _activation_summary(x):
    """Helper to create summaries for activations.
    
    Creates a summary that provides a histogram of activations.
    Creates a summary that measures the sparsity of activations.
    
    Args:
      x: Tensor
    Returns:
      nothing
    """
    # Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
    # session. This helps the clarity of presentation on tensorboard.
    tensor_name = re.sub('%s_[0-9]*/' % TOWER_NAME, '', x.op.name)
    #tf.histogram_summary(tensor_name + '/activations', x)
    tf.summary.scalar(tensor_name + '/sparsity', tf.nn.zero_fraction(x))


def _variable_on_cpu(name, shape, initializer, dtype):
    """Helper to create a Variable stored on CPU memory.
    
    Args:
      name: name of the variable
      shape: list of ints
      initializer: initializer for Variable
    
    Returns:
      Variable Tensor
    """
    with tf.device('/cpu:0'):
        var = tf.get_variable(name, shape, initializer=initializer, dtype=dtype)
    return var


def _variable_with_weight_decay(name, shape, initializer, dtype, wd, trainable=True):
    """Helper to create an initialized Variable with weight decay.
    
    Note that the Variable is initialized with a truncated normal distribution.
    A weight decay is added only if one is specified.
    
    Args:
      name: name of the variable
      shape: list of ints
      stddev: standard deviation of a truncated Gaussian
      wd: add L2Loss weight decay multiplied by this float. If None, weight
          decay is not added for this Variable.
    
    Returns:
      Variable Tensor
    """     
    # tf.truncated_normal_initializer(stddev=stddev, dtype=dtype)
    if isinstance(initializer, np.ndarray):
        var = tf.get_variable(name, initializer=initializer, dtype=dtype, trainable=trainable)
    else:
        var = tf.get_variable(name, shape, initializer=initializer, dtype=dtype, trainable=trainable)
    #if wd is not None:
    #    weight_decay = tf.mul(tf.nn.l2_loss(var), wd, name='weight_loss')
    #    tf.add_to_collection('losses', weight_decay)
        
    return var



def batch_norm(name, tensorConv, dtype):
    with tf.variable_scope(name):
        # Calc batch mean for parallel module
        batchMean, batchVar = tf.nn.moments(tensorConv, axes=[0]) # moments along x,y
        scale = tf.Variable(tf.ones(tensorConv.get_shape()[-1]))
        beta = tf.Variable(tf.zeros(tensorConv.get_shape()[-1]))
        epsilon = 1e-3
        if dtype is tf.float16:
            scale = tf.cast(scale, tf.float16)
            beta = tf.cast(beta, tf.float16)
            epsilon = tf.cast(epsilon, tf.float16)
        batchNorm = tf.nn.batch_normalization(tensorConv, batchMean, batchVar, beta, scale, epsilon)
    return batchNorm
    

def twin_correlation(name, prevLayerOut, prevLayerDims, d, s2, **kwargs):
    dtype = tf.float16 if kwargs.get('usefp16') else tf.float32

    D = (2*d)+1
    numParallelModules = kwargs.get('numParallelModules') # 2
    # Input from Twin network -> numParallelModules = 2
    # Split tensor through last dimension into numParallelModules tensors
    prevLayerOut = tf.split(3, numParallelModules, prevLayerOut)
    prevLayerIndivDims = prevLayerDims / numParallelModules

    # f1 is a list of size batchSize,x,y of [1,1,c,1] tensors  => last 1 is for the output size
    prevLayerOut[0] = tf.unpack(prevLayerOut[0], axis=0) # batches seperate
    for i in range(len(prevLayerOut[0])):
        prevLayerOut[0][i] = tf.split(0, prevLayerOut[0][i].get_shape()[0], prevLayerOut[0][i]) # X seperate
        for j in range(len(prevLayerOut[0][i])):
            prevLayerOut[0][i][j] = tf.split(1, prevLayerOut[0][i][j].get_shape()[1], prevLayerOut[0][i][j]) # Y seperate
            for k in range(len(prevLayerOut[0][i][j])):
                prevLayerOut[0][i][j][k] = tf.reshape(prevLayerOut[0][i][j][k], [1, 1, int(prevLayerOut[0][i][j][k].get_shape()[2]), 1])
                prevLayerOut[0][i][j][k].set_shape([1, 1, int(prevLayerOut[0][i][j][k].get_shape()[2]), 1])

    # padd f2 with 20 zeros on each side
    # output is batch x [1, x+2d, y+2d, c]
    prevLayerOut[1] = tf.split(0, prevLayerOut[1].get_shape()[0], prevLayerOut[1]) # batches seperate
    for i in range(len(prevLayerOut[1])):
        prevLayerOut[1][i] = tf.pad(prevLayerOut[1][i], [[0,0],[d,d],[d,d],[0,0]], mode='CONSTANT', name=None)

    # correlation
    with tf.variable_scope(name):
        with tf.variable_scope('corr1x1') as scope:
            for batches in range(len(prevLayerOut[0])): # batches
                for i in range(len(prevLayerOut[0][batches])): # X
                    for j in range(len(prevLayerOut[0][batches][i])): # Y
                        # kernel_shape = width, height, dims (input), dims (output) [1,1,c,1]
                        f1kernel = prevLayerOut[0][batches][i][j]
                        ##print(f1kernel.get_shape())
                        # inputs of [1, x+2d, y+2d, c]
                        # Select square subtensor centered at i,j with square size D=2*d+1 and stride 2 in each axis with complete channel data
                        # if d=20, s2=2 => f2box = [1,21,21,1]
                        f2box = tf.strided_slice(prevLayerOut[1][batches], [0, i, j], [1, i+D, j+D], [1, s2, s2])
                        ##print(f2box.get_shape())
                        # corrsingle is a [1,21,21,1]
                        # reshape to [1,1,1,21*21] or [1,1,1,441]
                        corrSingle = tf.reshape(tf.nn.conv2d(f2box, f1kernel, [1, 1, 1, 1], padding='SAME'), [1, 1, 1, (d+1)*(d+1)])
                        # concat along height dimension
                        if j == 0:
                            corrBox = corrSingle
                        else:
                            corrBox = tf.concat(2, [corrBox, corrSingle])
                    # concat along width dimension
                    if i == 0:
                        resultTensor = corrBox
                    else:
                        resultTensor = tf.concat(1, [resultTensor, corrBox])
                # concat along batches
                if batches == 0:
                    resultBatch = resultTensor
                else:
                    resultBatch = tf.concat(0, [resultBatch, resultTensor])
    #return resultTensor
    return resultBatch, D*D


def conv_fire_parallel_module(name, prevLayerOut, prevLayerDims, fireDimsSingleModule, wd=None, **kwargs):
    """
    Input Args:
        name:               scope name
        prevLayerOut:       output tensor of previous layer
        prevLayerDims:      size of the last (3rd) dimension in prevLayerOut
        numParallelModules: number of parallel modules and parallel data in prevLayerOut
        fireDimsSingleModule:     number of output dimensions for each parallel module
    """
    USE_FP_16 = kwargs.get('usefp16')
    dtype = tf.float16 if USE_FP_16 else tf.float32

    existingParams = kwargs.get('existingParams')

    numParallelModules = kwargs.get('numParallelModules') # 2
    # Twin network -> numParallelModules = 2
    # Split tensor through last dimension into numParallelModules tensors
    prevLayerOut = tf.split(3, numParallelModules, prevLayerOut)
    prevLayerIndivDims = prevLayerDims / numParallelModules

    with tf.variable_scope(name):
        with tf.variable_scope('cnn3x3') as scope:
            layerName = scope.name.replace("/", "_")
            #kernel = _variable_with_weight_decay('weights',
            #                                     shape=[3, 3, prevLayerIndivDims, fireDimsSingleModule['cnn3x3']],
            #                                     initializer=existingParams[layerName]['weights'] if (existingParams is not None and
            #                                                                                         layerName in existingParams) else
            #                                                    (tf.contrib.layers.xavier_initializer_conv2d() if kwargs.get('phase')=='train' else
            #                                                     tf.constant_initializer(0.0, dtype=dtype)),
            #                                     dtype=dtype,
            # wd=wd,
            #                                     trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
            #                                                                               layerName in existingParams) else True)
            stddev = np.sqrt(2/np.prod(prevLayerOut[0].get_shape().as_list()[1:]))
            kernel = _variable_with_weight_decay('weights',
                                                 shape=[3, 3, prevLayerIndivDims, fireDimsSingleModule['cnn3x3']],
                                                 initializer=existingParams[layerName]['weights'] if (existingParams is not None and
                                                                                                     layerName in existingParams) else
                                                                (tf.random_normal_initializer(stddev=stddev) if kwargs.get('phase')=='train' else
                                                                 tf.constant_initializer(0.0, dtype=dtype)),
                                                 dtype=dtype,
                                                 wd=wd,
                                                 trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
                                                                                           layerName in existingParams) else True)
            
            for prl in range(numParallelModules):
                conv = tf.nn.conv2d(prevLayerOut[prl], kernel, [1, 1, 1, 1], padding='SAME')
                
                if kwargs.get('weightNorm'):
                    # calc weight norm
                    conv = batch_norm('weight_norm', conv, dtype)
                
                if existingParams is not None and layerName in existingParams:
                    biases = tf.get_variable('biases',
                                             initializer=existingParams[layerName]['biases'], dtype=dtype)
                else:
                    biases = tf.get_variable('biases', [fireDimsSingleModule['cnn3x3']],
                                             initializer=tf.constant_initializer(0.0),
                                             dtype=dtype)

                bias = tf.nn.bias_add(conv, biases)
                convReluPrl = tf.nn.relu(conv, name=scope.name)
                # Concatinate results along last dimension to get one output tensor
                if prl is 0:
                    convRelu = convReluPrl
                else:    
                    convRelu = tf.concat(3, [convRelu, convReluPrl])

            _activation_summary(convRelu)

    return convRelu, numParallelModules*fireDimsSingleModule['cnn3x3']

def conv_fire_module(name, prevLayerOut, prevLayerDim, fireDims, wd=None, **kwargs):
    USE_FP_16 = kwargs.get('usefp16')
    dtype = tf.float16 if USE_FP_16 else tf.float32
    
    existingParams = kwargs.get('existingParams')
    
    with tf.variable_scope(name):
        with tf.variable_scope('cnn3x3') as scope:
            layerName = scope.name.replace("/", "_")
            #kernel = _variable_with_weight_decay('weights',
            #                                     shape=[3, 3, prevLayerDim, fireDims['cnn3x3']],
            #                                     initializer=existingParams[layerName]['weights'] if (existingParams is not None and
            #                                                                                         layerName in existingParams) else
            #                                                    (tf.contrib.layers.xavier_initializer_conv2d() if kwargs.get('phase')=='train' else
            #                                                     tf.constant_initializer(0.0, dtype=dtype)),
            #                                     dtype=dtype,
            # wd=wd,
            #                                     trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
            #                                                                               layerName in existingParams) else True)
            stddev = np.sqrt(2/np.prod(prevLayerOut.get_shape().as_list()[1:]))
            kernel = _variable_with_weight_decay('weights',
                                                 shape=[3, 3, prevLayerDim, fireDims['cnn3x3']],
                                                 initializer=existingParams[layerName]['weights'] if (existingParams is not None and
                                                                                                     layerName in existingParams) else
                                                                (tf.random_normal_initializer(stddev=stddev) if kwargs.get('phase')=='train' else
                                                                 tf.constant_initializer(0.0, dtype=dtype)),
                                                 dtype=dtype,
                                                 wd=wd,
                                                 trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
                                                                                           layerName in existingParams) else True)
            conv = tf.nn.conv2d(prevLayerOut, kernel, [1, 1, 1, 1], padding='SAME')

            if kwargs.get('weightNorm'):
                # calc weight norm
                conv = batch_norm('weight_norm', conv, dtype)

            if existingParams is not None and layerName in existingParams:
                biases = tf.get_variable('biases',
                                         initializer=existingParams[layerName]['biases'], dtype=dtype)
            else:
                biases = tf.get_variable('biases', [fireDims['cnn3x3']],
                                         initializer=tf.constant_initializer(0.0),
                                         dtype=dtype)

            conv = tf.nn.bias_add(conv, biases)
            convRelu = tf.nn.relu(conv, name=scope.name)
            _activation_summary(convRelu)

        return convRelu, fireDims['cnn3x3']
        
def conv_fire_residual_module(name, prevLayerOut, prevLayerDim, historicLayerOut, historicLayerDim, fireDims, wd=None, **kwargs):
    USE_FP_16 = kwargs.get('usefp16')
    dtype = tf.float16 if USE_FP_16 else tf.float32
    
    existingParams = kwargs.get('existingParams')
    
    # output depth of the convolution should be same as historic
    if fireDims != historicLayerDim:
        # TO DO
        if kwargs.get('residualPadding') == "conv":
            # convlove historic data with a kernel of 1x1 and output size of prevLayerDim
            historicLayerOut = historicLayerOut
        if kwargs.get('residualPadding') == "zeroPad":
            # zero pad current historicLayerOut to the size of prevLayerDim
            historicLayerOut = historicLayerOut
    
    with tf.variable_scope(name):
        with tf.variable_scope('cnn3x3') as scope:
            layerName = scope.name.replace("/", "_")
            #kernel = _variable_with_weight_decay('weights',
            #                                     shape=[3, 3, prevLayerDim, fireDims['cnn3x3']],
            #                                     initializer=existingParams[layerName]['weights'] if (existingParams is not None and
            #                                                                                         layerName in existingParams) else
            #                                                    (tf.contrib.layers.xavier_initializer_conv2d() if kwargs.get('phase')=='train' else
            #                                                     tf.constant_initializer(0.0, dtype=dtype)),
            #                                     dtype=dtype,
            # wd=wd,
            #                                     trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
            #                                                                               layerName in existingParams) else True)
            stddev = np.sqrt(2/np.prod(prevLayerOut.get_shape().as_list()[1:]))
            kernel = _variable_with_weight_decay('weights',
                                                 shape=[3, 3, prevLayerDim, fireDims['cnn3x3']],
                                                 initializer=existingParams[layerName]['weights'] if (existingParams is not None and
                                                                                                     layerName in existingParams) else
                                                                (tf.random_normal_initializer(stddev=stddev) if kwargs.get('phase')=='train' else
                                                                 tf.constant_initializer(0.0, dtype=dtype)),
                                                 dtype=dtype,
                                                 wd=wd,
                                                 trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
                                                                                           layerName in existingParams) else True)
            conv = tf.nn.conv2d(prevLayerOut, kernel, [1, 1, 1, 1], padding='SAME')

            if kwargs.get('weightNorm'):
                # calc weight norm
                conv = batch_norm('weight_norm', conv, dtype)

            # residual
            convRelu = tf.nn.relu(historicLayerOut+conv, name=scope.name)
            _activation_summary(convRelu)

        return convRelu, fireDims['cnn3x3']

def fc_fire_module(name, prevLayerOut, prevLayerDim, fireDims, wd=None, **kwargs):
    USE_FP_16 = kwargs.get('usefp16')
    dtype = tf.float16 if USE_FP_16 else tf.float32

    existingParams = kwargs.get('existingParams')

    with tf.variable_scope(name):
        with tf.variable_scope('fc') as scope:
            stddev = np.sqrt(2/np.prod(prevLayerOut.get_shape().as_list()[1:]))
            fcWeights = _variable_with_weight_decay('weights',
                                                    shape=[prevLayerDim, fireDims['fc']],
                                                    initializer=(tf.random_normal_initializer(stddev=stddev) if kwargs.get('phase')=='train'
                                                                   else tf.constant_initializer(0.0, dtype=dtype)),
                                                    dtype=dtype,
                                                    wd=wd,
                                                    trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
                                                                                           layerName in existingParams) else True)
            
            # prevLayerOut is [batchSize, HxWxD], matmul -> [batchSize, fireDims['fc']]
            fc = tf.matmul(prevLayerOut, fcWeights)

            if kwargs.get('weightNorm'):
                # calc weight norm
                fc = batch_norm('weight_norm', fc, dtype)

            biases = tf.get_variable('biases', fireDims['fc'],
                                     initializer=tf.constant_initializer(0.0), dtype=dtype)
            fc = tf.nn.bias_add(fc, biases)
            fcRelu = tf.nn.relu(fc, name=scope.name)
            _activation_summary(fcRelu)
        
        return fcRelu, fireDims['fc']

def fc_regression_module(name, prevLayerOut, prevLayerDim, fireDims, wd=None, **kwargs):
    USE_FP_16 = kwargs.get('usefp16')
    dtype = tf.float16 if USE_FP_16 else tf.float32

    existingParams = kwargs.get('existingParams')

    with tf.variable_scope(name):
        with tf.variable_scope('fc') as scope:
            stddev = np.sqrt(2/np.prod(prevLayerOut.get_shape().as_list()[1:]))
            fcWeights = _variable_with_weight_decay('weights',
                                                    shape=[prevLayerDim, fireDims['fc']],
                                                    initializer=(tf.random_normal_initializer(stddev=stddev) if kwargs.get('phase')=='train'
                                                                   else tf.constant_initializer(0.0, dtype=dtype)),
                                                    dtype=dtype,
                                                    wd=wd,
                                                    trainable=kwargs.get('tuneExistingWeights') if (existingParams is not None and 
                                                                                           layerName in existingParams) else True)
            
            # prevLayerOut is [batchSize, HxWxD], matmul -> [batchSize, fireDims['fc']]
            fc = tf.matmul(prevLayerOut, fcWeights)

            if kwargs.get('weightNorm'):
                # calc weight norm
                fc = batch_norm('weight_norm', fc, dtype)

            biases = tf.get_variable('biases', fireDims['fc'],
                                     initializer=tf.constant_initializer(0.0), dtype=dtype)
            fc = tf.nn.bias_add(fc, biases)
            _activation_summary(fc)
        
        return fc, fireDims['fc']

def loss(pred, tval, lossType):
    return loss_base.loss(pred, tval, lossType)

def train(loss, globalStep, **kwargs):
    if kwargs.get('optimizer') == 'MomentumOptimizer':
        optimizerParams = optimizer_params.get_momentum_optimizer_params(kwargs)
    if kwargs.get('optimizer') == 'AdamOptimizer':
        optimizerParams = optimizer_params.get_adam_optimizer_params(kwargs)
    if kwargs.get('optimizer') == 'GradientDescentOptimizer':
        optimizerParams = optimizer_params.get_gradient_descent_optimizer_params(kwargs)

    # Generate moving averages of all losses and associated summaries.
    lossAveragesOp = loss_base._add_loss_summaries(loss, kwargs.get('activeBatchSize', None))
    
    # Compute gradients.
    tvars = tf.trainable_variables()
    with tf.control_dependencies([lossAveragesOp]):
        if kwargs.get('optimizer') == 'AdamOptimizer':
            optim = tf.train.AdamOptimizer(learning_rate=optimizerParams['learningRate'], epsilon=optimizerParams['epsilon'])
        if kwargs.get('optimizer') == 'MomentumOptimizer':
            optim = tf.train.MomentumOptimizer(learning_rate=optimizerParams['learningRate'], momentum=optimizerParams['momentum'])
        if kwargs.get('optimizer') == 'GradientDescentOptimizer':
            optim = tf.train.GradientDescentOptimizer(learning_rate=optimizerParams['learningRate'])

        grads, norm = tf.clip_by_global_norm(tf.gradients(loss, tvars), kwargs.get('clipNorm'))

    # Apply gradients.
    #applyGradientOp = opt.apply_gradients(grads, global_step=globalStep)
    #train_op = opt.apply_gradients(gradsNvars, global_step=globalStep)
    opApplyGradients = optim.apply_gradients(zip(grads, tvars), global_step=globalStep)

        
    # Add histograms for trainable variables.
    for var in tf.trainable_variables():    
        tf.summary.histogram(var.op.name, var)
    
    # Add histograms for gradients.
    for grad, var in zip(grads, tvars):
        if grad is not None:
            tf.summary.histogram(var.op.name + '/gradients', grad)
    
    with tf.control_dependencies([opApplyGradients]):
        opTrain = tf.no_op(name='train')

    return opTrain

def test(loss, globalStep, **kwargs):
    # Generate moving averages of all losses and associated summaries.
    lossAveragesOp = loss_base._add_loss_summaries(loss, kwargs.get('activeBatchSize', None))
    
    with tf.control_dependencies([]):
        opTest = tf.no_op(name='test')

    return opTest