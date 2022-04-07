import argparse
import os
import sys
import glob
import time
import numpy as np
import psutil
import warnings
import oneflow as flow
import oneflow.nn as nn
from sklearn.metrics import roc_auc_score
from petastorm.reader import make_batch_reader

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir)))


def get_args(print_args=True):
    def int_list(x):
        return list(map(int, x.split(",")))

    def str_list(x):
        return list(map(str, x.split(",")))

    parser = argparse.ArgumentParser()

    parser.add_argument("--disable_fusedmlp", action="store_true", help="disable fused MLP or not")
    parser.add_argument("--embedding_vec_size", type=int, default=128)
    parser.add_argument("--dnn", type=int_list, default="1024,1024,512,256")
    parser.add_argument("--model_load_dir", type=str, default=None)
    parser.add_argument("--model_save_dir", type=str, default=None)
    parser.add_argument(
        "--save_initial_model", action="store_true", help="save initial model parameters or not.",
    )
    parser.add_argument(
        "--save_model_after_each_eval", action="store_true", help="save model after each eval.",
    )
    parser.add_argument("--data_dir", type=str, required=True)
    parser.add_argument("--eval_batches", type=int, default=1612, help="number of eval batches")
    parser.add_argument("--eval_batch_size", type=int, default=55296)
    parser.add_argument("--eval_interval", type=int, default=10000)
    parser.add_argument("--train_batch_size", type=int, default=55296)
    parser.add_argument("--learning_rate", type=float, default=24)
    parser.add_argument("--warmup_batches", type=int, default=2750)
    parser.add_argument("--decay_batches", type=int, default=27772)
    parser.add_argument("--decay_start", type=int, default=49315)
    parser.add_argument("--train_batches", type=int, default=75000)
    parser.add_argument("--loss_print_interval", type=int, default=1000)
    parser.add_argument(
        "--table_size_array",
        type=int_list,
        help="Embedding table size array for sparse fields",
        required=True,
    )
    parser.add_argument(
        "--persistent_path", type=str, required=True, help="path for persistent kv store",
    )
    parser.add_argument(
        "--persistent_path_fm", type=str, required=True, help="path for persistent kv store(FM component)",
    )
    parser.add_argument("--store_type", type=str, default="cached_host_mem")
    parser.add_argument("--cache_memory_budget_mb", type=int, default=8192)
    parser.add_argument("--amp", action="store_true", help="Run model with amp")
    parser.add_argument("--loss_scale_policy", type=str, default="static", help="static or dynamic")

    args = parser.parse_args()

    if print_args and flow.env.get_rank() == 0:
        _print_args(args)
    return args


def _print_args(args):
    """Print arguments."""
    print("------------------------ arguments ------------------------", flush=True)
    str_list = []
    for arg in vars(args):
        dots = "." * (48 - len(arg))
        str_list.append("  {} {} {}".format(arg, dots, getattr(args, arg)))
    for arg in sorted(str_list, key=lambda x: x.lower()):
        print(arg, flush=True)
    print("-------------------- end of arguments ---------------------", flush=True)


num_dense_fields = 13
num_sparse_fields = 26


class PNNDataReader(object):
    """A context manager that manages the creation and termination of a
    :class:`petastorm.Reader`.
    """

    def __init__(
        self,
        parquet_file_url_list,
        batch_size,
        num_epochs=1,
        shuffle_row_groups=True,
        shard_seed=1234,
        shard_count=1,
        cur_shard=0,
    ):
        self.parquet_file_url_list = parquet_file_url_list
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        self.shuffle_row_groups = shuffle_row_groups
        self.shard_seed = shard_seed
        self.shard_count = shard_count
        self.cur_shard = cur_shard

        fields = ["Label"]
        fields += [f"I{i+1}" for i in range(num_dense_fields)]
        fields += [f"C{i+1}" for i in range(num_sparse_fields)]
        self.fields = fields
        self.num_fields = len(fields)

    def __enter__(self):
        self.reader = make_batch_reader(
            self.parquet_file_url_list,
            workers_count=2,
            shuffle_row_groups=self.shuffle_row_groups,
            num_epochs=self.num_epochs,
            shard_seed=self.shard_seed,
            shard_count=self.shard_count,
            cur_shard=self.cur_shard,
        )
        self.loader = self.get_batches(self.reader)
        return self.loader

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.reader.stop()
        self.reader.join()

    def get_batches(self, reader, batch_size=None):
        if batch_size is None:
            batch_size = self.batch_size

        tail = None

        for rg in reader:
            rgdict = rg._asdict()
            rglist = [rgdict[field] for field in self.fields]
            pos = 0
            if tail is not None:
                pos = batch_size - len(tail[0])
                tail = list(
                    [
                        np.concatenate((tail[i], rglist[i][0 : (batch_size - len(tail[i]))]))
                        for i in range(self.num_fields)
                    ]
                )
                if len(tail[0]) == batch_size:
                    label = tail[0]
                    features = tail[1:]
                    tail = None
                    yield label, np.stack(features, axis=-1)
                else:
                    pos = 0
                    continue

            while (pos + batch_size) <= len(rglist[0]):
                label = rglist[0][pos : pos + batch_size]
                features = rglist[1:][pos: pos + batch_size]
                pos += batch_size
                yield label, np.stack(features, axis=-1)

            if pos != len(rglist[0]):
                tail = [rglist[i][pos:] for i in range(self.num_fields)]


def make_criteo_dataloader(data_path, batch_size, shuffle=True):
    """Make a Criteo Parquet DataLoader.
    :return: a context manager when exit the returned context manager, the reader will be closed.
    """
    files = ["file://" + name for name in glob.glob(f"{data_path}/*.parquet")]
    files.sort()

    world_size = flow.env.get_world_size()
    batch_size_per_proc = batch_size // world_size

    return PNNDataReader(
        files,
        batch_size_per_proc,
        None,  # TODO: iterate over all eval dataset
        shuffle_row_groups=shuffle,
        shard_seed=1234,
        shard_count=world_size,
        cur_shard=flow.env.get_rank(),
    )


class OneEmbedding(nn.Module):
    def __init__(
        self,
        table_name,
        embedding_vec_size,
        persistent_path,
        table_size_array,
        store_type,
        cache_memory_budget_mb,
    ):
        assert table_size_array is not None
        vocab_size = sum(table_size_array)

        scales = np.sqrt(1 / np.array(table_size_array))
        tables = [
            flow.one_embedding.make_table(
                flow.one_embedding.make_uniform_initializer(low=-scale, high=scale)
            )
            for scale in scales
        ]
        if store_type == "device_mem":
            store_options = flow.one_embedding.make_device_mem_store_options(
                persistent_path=persistent_path, capacity=vocab_size
            )
        elif store_type == "cached_host_mem":
            assert cache_memory_budget_mb > 0
            store_options = flow.one_embedding.make_cached_host_mem_store_options(
                cache_budget_mb=cache_memory_budget_mb,
                persistent_path=persistent_path,
                capacity=vocab_size,
            )
        elif store_type == "cached_ssd":
            assert cache_memory_budget_mb > 0
            store_options = flow.one_embedding.make_cached_ssd_store_options(
                cache_budget_mb=cache_memory_budget_mb,
                persistent_path=persistent_path,
                capacity=vocab_size,
            )
        else:
            raise NotImplementedError("not support", store_type)

        super(OneEmbedding, self).__init__()
        self.one_embedding = flow.one_embedding.MultiTableEmbedding(
            name=table_name,
            embedding_dim=embedding_vec_size,
            dtype=flow.float,
            key_type=flow.int64,
            tables=tables,
            store_options=store_options,
        )

    def forward(self, ids):
        return self.one_embedding.forward(ids)


class DenseLayer(nn.Module):
    def __init__(self, in_features: int, out_features: int, relu=True) -> None:
        super(DenseLayer, self).__init__()
        self.features = (
            nn.Sequential(nn.Linear(in_features, out_features), nn.ReLU(inplace=True))
            if relu
            else nn.Linear(in_features, out_features)
        )

    def forward(self, x: flow.Tensor) -> flow.Tensor:
        return self.features(x)


class DNN(nn.Module):
    def __init__(self, in_features: int, hidden_units, skip_final_activation=False, fused=True) -> None:
        super(DNN, self).__init__()
        if fused:
            self.linear_layers = nn.FusedMLP(
                in_features,
                hidden_units[:-1],
                hidden_units[-1],
                skip_final_activation=skip_final_activation
            )
        else:
            units = [in_features] + hidden_units
            num_layers = len(hidden_units)
            denses = [
                DenseLayer(units[i], units[i + 1], not skip_final_activation or (i + 1) < num_layers)
                for i in range(num_layers)
            ]
            self.linear_layers = nn.Sequential(*denses)

        for name, param in self.linear_layers.named_parameters():
            if "weight" in name:
                nn.init.normal_(param, 0.0, np.sqrt(2 / sum(param.shape)))
            elif "bias" in name:
                nn.init.normal_(param, 0.0, np.sqrt(1 / param.shape[0]))

    def forward(self, x: flow.Tensor) -> flow.Tensor:
        return self.linear_layers(x)


class LR(nn.Module):
    def __init__(
        self,
        persistent_path=None,
        table_size_array=None,
        one_embedding_store_type="cached_host_mem",
        cache_memory_budget_mb=8192,
        use_bias=True
    ):
        super(LR, self).__init__()
        self.bias = nn.Parameter(flow.tensor([1], dtype=flow.float32)) if use_bias else None
        self.embedding_layer = OneEmbedding(
            table_name="fm_lr_embedding",
            embedding_vec_size=1,
            persistent_path=persistent_path,
            table_size_array=table_size_array,
            store_type=one_embedding_store_type,
            cache_memory_budget_mb=cache_memory_budget_mb
        )

    def forward(self, x):
        # x = original ids
        # order-1 feature interaction
        embedded_x = self.embedding_layer(x)
        output = flow.sum(embedded_x, dim=1)
        if self.bias is not None:
            output += self.bias
        return output


class Interaction(nn.Module):
    def __init__(self, interaction_itself=False, num_fields=26):
        super(Interaction, self).__init__()
        self.interaction_itself = interaction_itself
        self.num_fields = num_fields
        
    def forward(self, embedded_x:flow.Tensor) -> flow.Tensor:
        sum_of_square = flow.sum(embedded_x, dim=1) ** 2
        square_of_sum = flow.sum(embedded_x ** 2, dim=1)
        bi_interaction = (sum_of_square - square_of_sum) * 0.5
        return flow.sum(bi_interaction, dim=-1).view(-1, 1)


class FM(nn.Module):
    def __init__(
        self, 
        persistent_path=None,
        table_size_array=None,
        one_embedding_store_type="cached_host_mem",
        cache_memory_budget_mb=8192,
        use_bias=True
    ):
        super(FM, self).__init__()
        self.interaction = Interaction(num_fields=num_dense_fields+num_sparse_fields)
        self.lr = LR(
            persistent_path=persistent_path,
            table_size_array=table_size_array,
            one_embedding_store_type=one_embedding_store_type,
            cache_memory_budget_mb=cache_memory_budget_mb,
            use_bias=use_bias
        )
    
    def forward(self, x:flow.Tensor, embedded_x:flow.Tensor) -> flow.Tensor:
        lr_out = self.lr(x)
        dot_sum = self.interaction(embedded_x)
        output = lr_out + dot_sum
        return output


# class PNNModule(nn.Module):
#     def __init__(
#         self,
#         embedding_vec_size=128,
#         dnn=[1024, 1024, 512, 256],
#         use_fusedmlp=True,
#         persistent_path=None,
#         persistent_path_fm=None,
#         table_size_array=None,
#         one_embedding_store_type="cached_host_mem",
#         cache_memory_budget_mb=8192,
#     ):
#         super(PNNModule, self).__init__()

#         self.embedding_layer = OneEmbedding(
#             table_name="sparse_embedding",
#             embedding_vec_size=embedding_vec_size,
#             persistent_path=persistent_path,
#             table_size_array=table_size_array,
#             store_type=one_embedding_store_type,
#             cache_memory_budget_mb=cache_memory_budget_mb
#         )

#         self.fm_layer = FM(
#             persistent_path=persistent_path_fm,
#             table_size_array=table_size_array,
#             one_embedding_store_type=one_embedding_store_type,
#             cache_memory_budget_mb=cache_memory_budget_mb,
#             use_bias=True
#         )

#         self.dnn_layer = DNN(
#             in_features=embedding_vec_size * (num_dense_fields + num_sparse_fields),
#             hidden_units=dnn + [1],
#             skip_final_activation=True,
#             fused=use_fusedmlp
#         )
#         self.sigmoid = nn.Sigmoid()

#     def forward(self, inputs) -> flow.Tensor:
#         embedded_x = self.embedding_layer(inputs)
#         fm_pred = self.fm_layer(inputs, embedded_x)
#         dnn_pred = self.dnn_layer(embedded_x.flatten(start_dim=1))
#         y_pred = self.sigmoid(fm_pred + dnn_pred)
#         return y_pred


class InnerProductLayer(nn.Module):
    def __init__(self, reduce_sum=True, device='cpu'):
        super(InnerProductLayer, self).__init__()
        self.reduce_sum = reduce_sum
        self.to(device)

    def forward(self, inputs):

        embed_list = inputs
        row = []
        col = []
        num_inputs = len(embed_list)

        for i in range(num_inputs - 1):
            for j in range(i + 1, num_inputs):
                row.append(i)
                col.append(j)
        p = flow.cat([embed_list[idx]
                       for idx in row], dim=1)  # batch num_pairs k
        q = flow.cat([embed_list[idx]
                       for idx in col], dim=1)

        inner_product = p * q
        if self.reduce_sum:
            inner_product = flow.sum(
                inner_product, dim=2, keepdim=True)
        return inner_product


class OutterProductLayer(nn.Module):
    def __init__(self, field_size, embedding_size, seed=1024, device='cpu'):
        super(OutterProductLayer, self).__init__()
    #    self.kernel_type = kernel_type
        self.kernel_type = 'mat'
        num_inputs = field_size
        num_pairs = int(num_inputs * (num_inputs - 1) / 2)
        embed_size = embedding_size
        if self.kernel_type == 'mat':
      
            self.kernel = nn.Parameter(flow.Tensor(
                embed_size, num_pairs, embed_size))

        elif self.kernel_type == 'vec':
            self.kernel = nn.Parameter(flow.Tensor(num_pairs, embed_size))

        elif self.kernel_type == 'num':
            self.kernel = nn.Parameter(flow.Tensor(num_pairs, 1))
        nn.init.xavier_uniform_(self.kernel)

        self.to(device)

    def forward(self, inputs):
        embed_list = inputs
        row = []
        col = []
        num_inputs = len(embed_list)
        for i in range(num_inputs - 1):
            for j in range(i + 1, num_inputs):
                row.append(i)
                col.append(j)
        p = flow.cat([embed_list[idx]
                       for idx in row], dim=1)  # batch num_pairs k
        q = flow.cat([embed_list[idx] for idx in col], dim=1)

        # -------------------------
        #if self.kernel_type == 'mat':
        if True:         

    # p.unsqueeze_(dim=1)
            # k     k* pair* k
            # batch * pair
            kp = flow.sum(

                # batch * pair * k

                flow.mul(

                    # batch * pair * k

                    flow.transpose(

                        # batch * k * pair

                        flow.sum(

                            # batch * k * pair * k

                            flow.mul(

                                p.unsqueeze(dim=1), self.kernel),

                            dim=-1),

                        2, 1),

                    q),

                dim=-1)
        else:
            # 1 * pair * (k or 1)

            k = flow.unsqueeze(self.kernel, 0)

            # batch * pair

            kp = flow.sum(p * q * k, dim=-1)

            # p q # b * p * k

        return kp

class PNNModule(nn.Module):
    def __init__(
        self,
        embedding_vec_size=128,
        dnn=[1024, 1024, 512, 256],
        use_fusedmlp=True,
        persistent_path=None,
        table_size_array=None,
        one_embedding_store_type="cached_host_mem",
        cache_memory_budget_mb=8192,
    ):
        super(PNNModule, self).__init__()

        self.embedding_layer = OneEmbedding(
            table_name="sparse_embedding",
            embedding_vec_size=embedding_vec_size,
            persistent_path=persistent_path,
            table_size_array=table_size_array,
            store_type=one_embedding_store_type,
            cache_memory_budget_mb=cache_memory_budget_mb
        )

        self.dnn_layer = DNN(
            in_features=embedding_vec_size * (num_dense_fields + num_sparse_fields),
            hidden_units=dnn + [1],
            skip_final_activation=True,
            fused=use_fusedmlp
        )
        self.sigmoid = nn.Sigmoid()
        self.innerproduct = InnerProductLayer()
        self.outterproduct = OutterProductLayer(num_dense_fields + num_sparse_fields, embedding_vec_size)

    def forward(self, inputs) -> flow.Tensor:


        embedded_x = self.embedding_layer(inputs)
        print(embedded_x.shape)
#        sparse_embedding_list, dense_value_list = self.input_from_feature_columns(X, self.dnn_feature_columns,                                                                                  self.embedding_dict)
#        linear_signal = flow.flatten(flow.cat(sparse_embedding_list), start_dim=1)

        inner_product = flow.flatten(self.innerproduct(embedded_x), start_dim=1)

       # outer_product = self.outterproduct(embedded_x)


        fm_pred = self.fm_layer(inputs, embedded_x)
        dnn_pred = self.dnn_layer(embedded_x.flatten(start_dim=1))
        y_pred = self.sigmoid(fm_pred + dnn_pred)
        return y_pred

    def input_from_feature_columns(self, X, sparse_feature_columns, dense_feature_columns, embedding_dict, support_dense=True):

        sparse_embedding_list = [embedding_dict[feat.embedding_name](
            X[:, self.feature_index[feat.name][0]:self.feature_index[feat.name][1]].long()) for
            feat in sparse_feature_columns]


        dense_value_list = [X[:, self.feature_index[feat.name][0]:self.feature_index[feat.name][1]] for feat in
                            dense_feature_columns]

        return sparse_embedding_list, dense_value_list

def make_pnn_module(args):
    model = PNNModule(
        embedding_vec_size=args.embedding_vec_size,
        dnn=args.dnn,
        use_fusedmlp=not args.disable_fusedmlp,
        persistent_path=args.persistent_path,
        # persistent_path_fm=args.persistent_path_fm,
        table_size_array=args.table_size_array,
        one_embedding_store_type=args.store_type,
        cache_memory_budget_mb=args.cache_memory_budget_mb,
    )
    return model


class PNNValGraph(flow.nn.Graph):
    def __init__(self, pnn_module, amp=False):
        super(PNNValGraph, self).__init__()
        self.module = pnn_module
        if amp:
            self.config.enable_amp(True)

    def build(self, features):
        predicts = self.module(features.to("cuda"))
        return predicts.to("cpu")


class PNNTrainGraph(flow.nn.Graph):
    def __init__(
        self, pnn_module, loss, optimizer, lr_scheduler=None, grad_scaler=None, amp=False,
    ):
        super(PNNTrainGraph, self).__init__()
        self.module = pnn_module
        self.loss = loss
        self.add_optimizer(optimizer, lr_sch=lr_scheduler)
        self.config.allow_fuse_model_update_ops(True)
        self.config.allow_fuse_add_to_output(True)
        self.config.allow_fuse_cast_scale(True)
        if amp:
            self.config.enable_amp(True)
            self.set_grad_scaler(grad_scaler)

    def build(self, labels, features):
        logits = self.module(features.to("cuda"))
        loss = self.loss(logits, labels.to("cuda"))
        reduce_loss = flow.mean(loss)
        reduce_loss.backward()
        return reduce_loss.to("cpu")


def make_lr_scheduler(args, optimizer):
    warmup_lr = flow.optim.lr_scheduler.LinearLR(
        optimizer, start_factor=0, total_iters=args.warmup_batches,
    )
    poly_decay_lr = flow.optim.lr_scheduler.PolynomialLR(
        optimizer, steps=args.decay_batches, end_learning_rate=0, power=2.0, cycle=False,
    )
    sequential_lr = flow.optim.lr_scheduler.SequentialLR(
        optimizer=optimizer,
        schedulers=[warmup_lr, poly_decay_lr],
        milestones=[args.decay_start],
        interval_rescaling=True,
    )
    return sequential_lr


def train(args): 
    rank = flow.env.get_rank()

    pnn_module = make_pnn_module(args)
    pnn_module.to_global(flow.env.all_device_placement("cuda"), flow.sbp.broadcast)

    opt = flow.optim.SGD(pnn_module.parameters(), lr=args.learning_rate)
    lr_scheduler = make_lr_scheduler(args, opt)
    loss = flow.nn.BCEWithLogitsLoss(reduction="none").to("cuda")

    if args.loss_scale_policy == "static":
        grad_scaler = flow.amp.StaticGradScaler(1024)
    else:
        grad_scaler = flow.amp.GradScaler(
            init_scale=1073741824, growth_factor=2.0, backoff_factor=0.5, growth_interval=2000,
        )
    
    eval_graph = PNNValGraph(pnn_module, args.amp)
    train_graph = PNNTrainGraph(pnn_module, loss, opt, lr_scheduler, grad_scaler, args.amp)

    pnn_module.train()
    step, last_step, last_time = -1, 0, time.time()
    with make_criteo_dataloader(f"{args.data_dir}/train", args.train_batch_size) as loader:
        for step in range(1, args.train_batches + 1):
            labels, features = batch_to_global(*next(loader))
            loss = train_graph(labels, features)
            if step % args.loss_print_interval == 0:
                loss = loss.numpy()
                if rank == 0:
                    latency_ms = 1000 * (time.time() - last_time) / (step - last_step)
                    last_step, last_time = step, time.time()
                    strtime = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"Rank[{rank}], Step {step}, Loss {loss:0.4f}, "
                        + f"Latency {latency_ms:0.3f} ms, {strtime}"
                    )

            if args.eval_interval > 0 and step % args.eval_interval == 0:
                auc = eval(args, eval_graph, step)
                if args.save_model_after_each_eval:
                    save_model(f"step_{step}_val_auc_{auc:0.5f}")
                pnn_module.train()
                last_time = time.time()

    if args.eval_interval > 0 and step % args.eval_interval != 0:
        auc = eval(args, eval_graph, step)
        if args.save_model_after_each_eval:
            save_model(f"step_{step}_val_auc_{auc:0.5f}")


def batch_to_global(np_label, np_features):
    def _np_to_global(np, dtype=flow.float):
        t = flow.tensor(np, dtype=dtype)
        return t.to_global(placement=flow.env.all_device_placement("cpu"), sbp=flow.sbp.split(0))
    labels = _np_to_global(np_label.reshape(-1, 1))
    features = _np_to_global(np_features, dtype=flow.int64)
    return labels, features


def eval(args, eval_graph, cur_step=0):
    if args.eval_batches <= 0:
        return
    eval_graph.module.eval()
    labels, preds = [], []
    eval_start_time = time.time()
    with make_criteo_dataloader(
        f"{args.data_dir}/test", args.eval_batch_size, shuffle=False
    ) as loader:
        num_eval_batches = 0
        for np_batch in loader:
            num_eval_batches += 1
            if num_eval_batches > args.eval_batches:
                break
            label, features = batch_to_global(*np_batch)
            pred = eval_graph(features) # Caution: sigmoid in module or only in eval?
            labels.append(label.numpy())
            preds.append(pred.numpy())

    auc = 0  # will be updated by rank 0 only
    rank = flow.env.get_rank()
    if rank == 0:
        labels = np.concatenate(labels, axis=0)
        preds = np.concatenate(preds, axis=0)
        eval_time = time.time() - eval_start_time
        auc_start_time = time.time()
        auc = roc_auc_score(labels, preds)
        auc_time = time.time() - auc_start_time

        host_mem_mb = psutil.Process().memory_info().rss // (1024 * 1024)
        stream = os.popen("nvidia-smi --query-gpu=memory.used --format=csv")
        device_mem_str = stream.read().split("\n")[rank + 1]

        strtime = time.strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"Rank[{rank}], Step {cur_step}, AUC {auc:0.5f}, Eval_time {eval_time:0.2f} s, "
            + f"AUC_time {auc_time:0.2f} s, Eval_samples {labels.shape[0]}, "
            + f"GPU_Memory {device_mem_str}, Host_Memory {host_mem_mb} MiB, {strtime}"
        )

    flow.comm.barrier()
    return auc

if __name__ == "__main__":
    os.system(sys.executable + " -m oneflow --doctor")
    flow.boxing.nccl.enable_all_to_all(True)
    args = get_args()
    train(args)
