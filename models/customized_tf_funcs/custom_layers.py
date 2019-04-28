from tensorflow.python.keras.layers import Layer
from utils.algebra import exp_mapping
from tensorflow.python.ops.array_ops import expand_dims, concat


class ForkLayer(Layer):
    def __init__(self, *args, **kwargs):
        super(ForkLayer, self).__init__(args, kwargs)

    def call(self, inputs, **kwargs):
        return inputs[:, :, 0:6, :] * 1, inputs[:, :, 6, :] * 1


class ForkLayerIMUInt(Layer):
    def __init__(self, window_len, state_len, name=None):
        super(ForkLayerIMUInt, self).__init__(name=name)
        self.imu_window_len = window_len
        self.state_len = state_len

    def call(self, inputs, **kwargs):
        return inputs[:, :self.imu_window_len, :6, :], \
               inputs[:, :self.imu_window_len, 6:, :], \
               inputs[:, self.imu_window_len:, 0, :]


class ExponentialRemappingLayer(Layer):
    def __init__(self, name=None):
        super(ExponentialRemappingLayer, self).__init__(name=name, trainable=False)

    def call(self, inputs, **kwargs):
        if not inputs.shape[0]:
            print(inputs.shape)
            return concat([inputs, expand_dims(inputs[:, 0], axis=1)], axis=1)

        q = exp_mapping(inputs[:, 6:9])
        return concat([inputs[:, :6], q], axis=1)