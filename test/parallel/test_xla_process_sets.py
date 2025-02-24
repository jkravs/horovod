# Copyright 2021 The TensorFlow Authors. All Rights Reserved.
# Modifications copyright (C) 2018 Uber Technologies, Inc.
# Modifications copyright (C) 2019 Intel Corporation
# Modifications copyright (C) 2020, NVIDIA CORPORATION. All rights reserved.
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
# =============================================================================

"""Tests for horovod.tensorflow.xla_mpi_ops that add/remove process sets after initialization.

With TensorFlow 2.9 and MPI the option HOROVOD_DYNAMIC_PROCESS_SETS has been observed to cause significant
slowdowns in all Horovod operations, especially on GPU-equipped AWS instances. For that reason we separate
out tests that depend on that setting to this script.
"""

from distutils.version import LooseVersion

import itertools
import numpy as np
import os
import platform
import math
import pytest
import sys

# Enable HVD XLA ops so that tf.function(jit_compile=True) works. This
# environment variable needs to be set up before loading Tensorflow, because
# it is needed to tell XLA to register the ops through C++ static
# initialization.
os.environ["HOROVOD_ENABLE_XLA_OPS"] = "1"

import tensorflow as tf
from horovod.tensorflow.util import _executing_eagerly

sys.path.append(os.path.join(os.path.dirname(__file__), os.pardir, 'utils'))

import horovod.tensorflow as hvd

from base_test_tensorflow import *

_IS_TF26 = LooseVersion(tf.__version__) >= LooseVersion('2.6.0')

# Set environment variable to enable adding/removing process sets after
# initializing Horovod.
os.environ["HOROVOD_DYNAMIC_PROCESS_SETS"] = "1"


@pytest.mark.skipif(not _IS_TF26, reason='TF2.6+ is required')
class XLAProcessSetsTests(BaseTensorFlowTests):
    """
    Tests for ops in horovod.tensorflow that add/remove process sets after initialization.
    """

    def __init__(self, *args, **kwargs):
        super(XLAProcessSetsTests, self).__init__(*args, **kwargs)

    def test_horovod_allreduce_gpu_process_sets(self):
        """ Test on XLA/GPU that allreduce correctly sums if restricted to non-global process sets"""
        # Only do this test if there are GPUs available.
        if not tf.test.is_gpu_available(cuda_only=True):
            self.skipTest(("No GPUs available"))

        if int(os.environ.get('HOROVOD_MIXED_INSTALL', 0)):
            # Skip if compiled with CUDA but without HOROVOD_GPU_OPERATIONS.
            self.skipTest("Not compiled with HOROVOD_GPU_OPERATIONS")

        hvd.init()
        local_rank = hvd.local_rank()
        rank = hvd.rank()
        size = hvd.size()

        even_ranks = [rk for rk in range(0, size) if rk % 2 == 0]
        odd_ranks = [rk for rk in range(0, size) if rk % 2 == 1]

        even_set = hvd.add_process_set(even_ranks)
        odd_set = hvd.add_process_set(odd_ranks)

        def allreduce_gpu_process_set(self, dtype, dim):
            even_rank_tensor = self.random_uniform(
                [17] * dim, -100, 100, dtype=dtype)
            odd_rank_tensor = self.random_uniform(
                [17] * dim, -100, 100, dtype=dtype)
            if rank in even_ranks:
                summed = hvd.allreduce(
                    even_rank_tensor,
                    average=False,
                    process_set=even_set)
                multiplied = even_rank_tensor * len(even_ranks)
            if rank in odd_ranks:
                summed = hvd.allreduce(
                    odd_rank_tensor, average=False, process_set=odd_set)
                multiplied = odd_rank_tensor * len(odd_ranks)
            max_difference = tf.reduce_max(tf.abs(summed - multiplied))
            return max_difference

        dtypes = [tf.int32, tf.int64, tf.float16, tf.float32, tf.float64]
        dims = [1, 2, 3]
        for dtype, dim in itertools.product(dtypes, dims):
            with tf.device("/gpu:%d" % local_rank):
                max_difference = tf.function(
                    allreduce_gpu_process_set, jit_compile=True)(self, dtype, dim)

            # Threshold for floating point equality depends on number of
            # ranks, since we're comparing against precise multiplication.
            max_process_set_size = max(len(even_ranks), len(odd_ranks))
            if max_process_set_size <= 3 or dtype in [tf.int32, tf.int64]:
                threshold = 0
            elif max_process_set_size < 10:
                threshold = 1e-4
            elif max_process_set_size < 15:
                threshold = 5e-4
            else:
                self.skipTest(
                    "Horovod cluster too large for precise multiplication comparison")

            diff = self.evaluate(max_difference)
            self.assertTrue(diff <= threshold,
                            "hvd.allreduce produces incorrect results")

        hvd.remove_process_set(odd_set)
        hvd.remove_process_set(even_set)
