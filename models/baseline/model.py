import tensorflow as tf
import tensorflow.keras as K
import numpy as np
import pickle


class PreNormException(Exception):
    pass

class PreNormLayer(K.layers.Layer):
    """
    Our pre-normalization layer, whose purpose is to normalize an input layer
    to zero mean and unit variance to speed-up and stabilize GCN training. The
    layer's parameters are aimed to be computed during the pre-training phase.
    """

    def __init__(self, n_units, shift=True, scale=True):
        super().__init__()
        assert shift or scale

        if shift:
            self.shift = self.add_weight(
                name=f'{self.name}/shift',
                shape=(n_units,),
                trainable=False,
                initializer=tf.keras.initializers.constant(value=np.zeros((n_units,)),
                dtype=tf.float32),
            )
        else:
            self.shift = None

        if scale:
            self.scale = self.add_weight(
                name=f'{self.name}/scale',
                shape=(n_units,),
                trainable=False,
                initializer=tf.keras.initializers.constant(value=np.ones((n_units,)),
                dtype=tf.float32),
            )
        else:
            self.scale = None

        self.n_units = n_units
        self.waiting_updates = False
        self.received_updates = False

    def build(self, input_shapes):
        self.built = True

        # 신경망에서 사용 시에는 pre_training을 통해 결정된 shift 및 scale 값으로 prenorm을 수행한다.
    def call(self, input):
        if self.waiting_updates:
            self.update_stats(input)
            self.received_updates = True
            raise PreNormException

        if self.shift is not None:
            input = input + self.shift

        if self.scale is not None:
            input = input * self.scale

        return input

    def start_updates(self):
        """
        Initializes the pre-training phase.
        """
        self.avg = 0
        self.var = 0
        self.m2 = 0
        self.count = 0
        self.waiting_updates = True
        self.received_updates = False

    def update_stats(self, input):
        """
        Online mean and variance estimation. See: Chan et al. (1979) Updating
        Formulae and a Pairwise Algorithm for Computing Sample Variances.
        https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Online_algorithm
        """
        assert self.n_units == 1 or input.shape[-1] == self.n_units, f"Expected input dimension of size {self.n_units}, got {input.shape[-1]}."

        # tf.reshape는 행렬과 출력 shape를 받아서 입력된 행렬을 해당 shape로 변환해준다.
        # shape에는 1개의 -1 값이 존재할 수 있다. -1은 자동으로 값을 대입하라는 뜻이다.
        # 만약 input이 4*6 행렬이고 출력 shape이 [-1, 8]로 지정되어 있으면 24/8=3이므로 출력 shape은 [3, 8]이 된다.
        input = tf.reshape(input, [-1, self.n_units])
        
        # tf.reduce_mean에서 2번째 인자는 axis 값이다.
        # 예를 들어, axis=0이고, 입력 행렬이 2*2 크기라고 하자.
        # 이 경우, 값은 [0,0], [0,1], [1,0], [1,1]의 4개의 위치에 있다.
        # 첫 축(차원)에 대해서 축소를 하므로 [0,0]과 [1,0]의 평균을 0, [0,1]과 [1,1]을 1의 위치에 둬서 1*2 크기의 행렬로 만든다.
        sample_avg = tf.reduce_mean(input, 0)
        
        # 데이터들에 대한 분산 값을 구한다.
        sample_var = tf.reduce_mean((input - sample_avg) ** 2, axis=0)
        sample_count = tf.cast(tf.size(input=input) / self.n_units, tf.float32)

        # delta는 기존 평균과 새 평균의 차이이다.
        delta = sample_avg - self.avg

        # 분산 * 수는 편차 제곱의 합과 같다.
        # 이는 기존 데이터 편차 제곱의 합과 현재 데이터 편차 제곱의 합을 더하고, 평균 차이의 제곱과 데이터 수들의 곱을 곱한 것을 데이터 수로 나눈 값을 더한 것이다.
        # m2를 delta에 대해서 미분하면 delta에 두 수의 조화평균을 곱한 값이 된다. 
        self.m2 = self.var * self.count + sample_var * sample_count + delta ** 2 * self.count * sample_count / (
                self.count + sample_count)

        # pre-training을 수행할 때마다 해당하는 데이터 수만큼 train 한 셈이다.
        self.count += sample_count
        # 평균은 전체 확인한 데이터 수 중 현재 데이터의 비율에 대해서 delta 만큼 반영해준다.
        self.avg += delta * sample_count / self.count
        # 분산에 대해서는 들어 있는 데이터들의 편차 제곱의 총합과 데이터 수의 조화평균의 적분의 합에 해당하는 m2를 사용한다.
        self.var = self.m2 / self.count if self.count > 0 else 1

    def stop_updates(self):
        """
        Ends pre-training for that layer, and fixes the layers's parameters.        
        """
        
        # shift 사용 시 평균 값을 대신 사용한다.
        assert self.count > 0
        if self.shift is not None:
            self.shift.assign(-self.avg)
        
        # scale 값으로는 표준 편차를 사용한다.
        if self.scale is not None:
            self.var = tf.where(tf.equal(self.var, 0), tf.ones_like(self.var), self.var)  # NaN check trick
            self.scale.assign(1 / np.sqrt(self.var))
        
        del self.avg, self.var, self.m2, self.count
        self.waiting_updates = False
        self.trainable = False


class BipartiteGraphConvolution(K.Model):
    """
    Partial bipartite graph convolution (either left-to-right or right-to-left).
    """

    def __init__(self, emb_size, activation, initializer, right_to_left=False):
        super().__init__()
        self.emb_size = emb_size
        self.activation = activation
        self.initializer = initializer
        self.right_to_left = right_to_left

        # feature layers
        # bipartite 그래프는 이분그래프라고도 한다. feature_module_left는 이분 그래프의 좌측에 해당하는 노드들을 embedding한다.
        self.feature_module_left = K.Sequential([
            K.layers.Dense(units=self.emb_size, activation=None, use_bias=True, kernel_initializer=self.initializer)
        ])
        # feature_module_edge는 이분 그래프의 간선들을 embedding한다.
        self.feature_module_edge = K.Sequential([
            K.layers.Dense(units=self.emb_size, activation=None, use_bias=False, kernel_initializer=self.initializer)
        ])
        # feature_module_right는 이분 그래프의 우측에 해당하는 노드들을 embedding한다.
        self.feature_module_right = K.Sequential([
            K.layers.Dense(units=self.emb_size, activation=None, use_bias=False, kernel_initializer=self.initializer)
        ])
        # feature_module_final은 ?
        self.feature_module_final = K.Sequential([
            PreNormLayer(1, shift=False),  # normalize after summation trick
            K.layers.Activation(self.activation),
            K.layers.Dense(units=self.emb_size, activation=None, kernel_initializer=self.initializer)
        ])

        self.post_conv_module = K.Sequential([
            PreNormLayer(1, shift=False),  # normalize after convolution
        ])

        # output_layers
        self.output_module = K.Sequential([
            K.layers.Dense(units=self.emb_size, activation=None, kernel_initializer=self.initializer),
            K.layers.Activation(self.activation),
            K.layers.Dense(units=self.emb_size, activation=None, kernel_initializer=self.initializer),
        ])

    def build(self, input_shapes):
        l_shape, ei_shape, ev_shape, r_shape = input_shapes

        self.feature_module_left.build(l_shape)
        self.feature_module_edge.build(ev_shape)
        self.feature_module_right.build(r_shape)
        self.feature_module_final.build([None, self.emb_size])
        self.post_conv_module.build([None, self.emb_size])
        self.output_module.build([None, self.emb_size + (l_shape[1] if self.right_to_left else r_shape[1])])
        self.built = True

    def call(self, inputs, training):
        """
        Perfoms a partial graph convolution on the given bipartite graph.

        Inputs
        ------
        left_features: 2D float tensor
            Features of the left-hand-side nodes in the bipartite graph
        edge_indices: 2D int tensor
            Edge indices in left-right order
        edge_features: 2D float tensor
            Features of the edges
        right_features: 2D float tensor
            Features of the right-hand-side nodes in the bipartite graph
        scatter_out_size: 1D int tensor
            Output size (left_features.shape[0] or right_features.shape[0], unknown at compile time)

        Other parameters
        ----------------
        training: boolean
            Training mode indicator
        """
        left_features, edge_indices, edge_features, right_features, scatter_out_size = inputs

        if self.right_to_left:
            scatter_dim = 0
            prev_features = left_features
        else:
            scatter_dim = 1
            prev_features = right_features

        # compute joint features
        joint_features = self.feature_module_final(
            tf.gather(
                self.feature_module_left(left_features),
                axis=0,
                indices=edge_indices[0]
            ) +
            self.feature_module_edge(edge_features) +
            tf.gather(
                self.feature_module_right(right_features),
                axis=0,
                indices=edge_indices[1])
        )

        # perform convolution
        conv_output = tf.scatter_nd(
            updates=joint_features,
            indices=tf.expand_dims(edge_indices[scatter_dim], axis=1),
            shape=[scatter_out_size, self.emb_size]
        )
        conv_output = self.post_conv_module(conv_output)

        # apply final module
        output = self.output_module(tf.concat([
            conv_output,
            prev_features,
        ], axis=1))

        return output


class BaseModel(K.Model):
    """
    Our base model class, which implements basic save/restore and pre-training
    methods.
    """

    def pre_train_init(self):
        self.pre_train_init_rec(self, self.name)

    @staticmethod
    def pre_train_init_rec(model, name):
        for layer in model.layers:
            if isinstance(layer, K.Model):
                BaseModel.pre_train_init_rec(layer, f"{name}/{layer.name}")
            elif isinstance(layer, PreNormLayer):
                layer.start_updates()

    def pre_train_next(self):
        return self.pre_train_next_rec(self, self.name)

    @staticmethod
    def pre_train_next_rec(model, name):
        for layer in model.layers:
            if isinstance(layer, K.Model):
                result = BaseModel.pre_train_next_rec(layer, f"{name}/{layer.name}")
                if result is not None:
                    return result
            elif isinstance(layer, PreNormLayer) and layer.waiting_updates and layer.received_updates:
                layer.stop_updates()
                return layer, f"{name}/{layer.name}"
        return None

    def pre_train(self, *args, **kwargs):
        try:
            self.call(*args, **kwargs)
            return False
        except PreNormException:
            return True

    def save_state(self, path):
        with open(path, 'wb') as f:
            for v_name in self.variables_topological_order:
                v = [v for v in self.variables if v.name == v_name][0]
                pickle.dump(v.numpy(), f)

    def restore_state(self, path):
        with open(path, 'rb') as f:
            for v_name in self.variables_topological_order:
                v = [v for v in self.variables if v.name == v_name][0]
                v.assign(pickle.load(f))


class GCNPolicy(BaseModel):
    """
    Our bipartite Graph Convolutional neural Network (GCN) model.
    """

    def __init__(self):
        super().__init__()

        self.emb_size = 64
        self.cons_nfeats = 5
        self.edge_nfeats = 1
        self.var_nfeats = 19

        self.activation = K.activations.relu
        self.initializer = K.initializers.Orthogonal()

        # CONSTRAINT EMBEDDING
        self.cons_embedding = K.Sequential([
            PreNormLayer(n_units=self.cons_nfeats),
            K.layers.Dense(units=self.emb_size, activation=self.activation, kernel_initializer=self.initializer),
            K.layers.Dense(units=self.emb_size, activation=self.activation, kernel_initializer=self.initializer),
        ])

        # EDGE EMBEDDING
        self.edge_embedding = K.Sequential([
            PreNormLayer(self.edge_nfeats),
        ])

        # VARIABLE EMBEDDING
        self.var_embedding = K.Sequential([
            PreNormLayer(n_units=self.var_nfeats),
            K.layers.Dense(units=self.emb_size, activation=self.activation, kernel_initializer=self.initializer),
            K.layers.Dense(units=self.emb_size, activation=self.activation, kernel_initializer=self.initializer),
        ])

        # GRAPH CONVOLUTIONS
        self.conv_v_to_c = BipartiteGraphConvolution(self.emb_size, self.activation, self.initializer, right_to_left=True)
        self.conv_c_to_v = BipartiteGraphConvolution(self.emb_size, self.activation, self.initializer)

        # OUTPUT
        self.output_module = K.Sequential([
            K.layers.Dense(units=self.emb_size, activation=self.activation, kernel_initializer=self.initializer),
            K.layers.Dense(units=1, activation=None, kernel_initializer=self.initializer, use_bias=False),
        ])

        # build model right-away
        self.build([
            (None, self.cons_nfeats),
            (2, None),
            (None, self.edge_nfeats),
            (None, self.var_nfeats),
            (None, ),
            (None, ),
        ])

        # save / restore fix
        self.variables_topological_order = [v.name for v in self.variables]

        # save input signature for compilation
        self.input_signature = [
            (
                tf.contrib.eager.TensorSpec(shape=[None, self.cons_nfeats], dtype=tf.float32),
                tf.contrib.eager.TensorSpec(shape=[2, None], dtype=tf.int32),
                tf.contrib.eager.TensorSpec(shape=[None, self.edge_nfeats], dtype=tf.float32),
                tf.contrib.eager.TensorSpec(shape=[None, self.var_nfeats], dtype=tf.float32),
                tf.contrib.eager.TensorSpec(shape=[None], dtype=tf.int32),
                tf.contrib.eager.TensorSpec(shape=[None], dtype=tf.int32),
            ),
            tf.contrib.eager.TensorSpec(shape=[], dtype=tf.bool),
        ]

    def build(self, input_shapes):
        c_shape, ei_shape, ev_shape, v_shape, nc_shape, nv_shape = input_shapes
        emb_shape = [None, self.emb_size]

        if not self.built:
            self.cons_embedding.build(c_shape)
            self.edge_embedding.build(ev_shape)
            self.var_embedding.build(v_shape)
            self.conv_v_to_c.build((emb_shape, ei_shape, ev_shape, emb_shape))
            self.conv_c_to_v.build((emb_shape, ei_shape, ev_shape, emb_shape))
            self.output_module.build(emb_shape)
            self.built = True

    @staticmethod
    def pad_output(output, n_vars_per_sample, pad_value=-1e8):
        n_vars_max = tf.reduce_max(n_vars_per_sample)

        output = tf.split(
            value=output,
            num_or_size_splits=n_vars_per_sample,
            axis=1,
        )
        output = tf.concat([
            tf.pad(
                x,
                paddings=[[0, 0], [0, n_vars_max - tf.shape(x)[1]]],
                mode='CONSTANT',
                constant_values=pad_value)
            for x in output
        ], axis=0)

        return output

    def call(self, inputs, training):
        """
        Accepts stacked mini-batches, i.e. several bipartite graphs aggregated
        as one. In that case the number of variables per samples has to be
        provided, and the output consists in a padded dense tensor.

        Parameters
        ----------
        inputs: list of tensors
            Model input as a bipartite graph. May be batched into a stacked graph.

        Inputs
        ------
        constraint_features: 2D float tensor
            Constraint node features (n_constraints x n_constraint_features)
        edge_indices: 2D int tensor
            Edge constraint and variable indices (2, n_edges)
        edge_features: 2D float tensor
            Edge features (n_edges, n_edge_features)
        variable_features: 2D float tensor
            Variable node features (n_variables, n_variable_features)
        n_cons_per_sample: 1D int tensor
            Number of constraints for each of the samples stacked in the batch.
        n_vars_per_sample: 1D int tensor
            Number of variables for each of the samples stacked in the batch.

        Other parameters
        ----------------
        training: boolean
            Training mode indicator
        """
        constraint_features, edge_indices, edge_features, variable_features, n_cons_per_sample, n_vars_per_sample = inputs
        n_cons_total = tf.reduce_sum(n_cons_per_sample)
        n_vars_total = tf.reduce_sum(n_vars_per_sample)

        # EMBEDDINGS
        constraint_features = self.cons_embedding(constraint_features)
        edge_features = self.edge_embedding(edge_features)
        variable_features = self.var_embedding(variable_features)

        # GRAPH CONVOLUTIONS
        constraint_features = self.conv_v_to_c((
            constraint_features, edge_indices, edge_features, variable_features, n_cons_total), training)
        constraint_features = self.activation(constraint_features)

        variable_features = self.conv_c_to_v((
            constraint_features, edge_indices, edge_features, variable_features, n_vars_total), training)
        variable_features = self.activation(variable_features)

        # OUTPUT
        output = self.output_module(variable_features)
        output = tf.reshape(output, [1, -1])

        if n_vars_per_sample.shape[0] > 1:
            output = self.pad_output(output, n_vars_per_sample)

        return output


