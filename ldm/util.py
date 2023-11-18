import importlib

import torch
import numpy as np
from collections import abc
from einops import rearrange
from functools import partial

import multiprocessing as mp
from multiprocessing import Pool
from threading import Thread
from queue import Queue

from inspect import isfunction
from PIL import Image, ImageDraw, ImageFont
import os

from slack_sdk import WebClient

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
if SLACK_BOT_TOKEN:
    client = WebClient(SLACK_BOT_TOKEN)
    try:
        auth_test = client.auth_test()
        bot_user_id = auth_test["user_id"]
        print(f"SLACK_BOT_TOKEN is valid, app's bot user: {bot_user_id}")
    except Exception:
        SLACK_BOT_TOKEN = None
        print("WARNING: Invalid SLACK_BOT_TOKEN, slack upload will be disabled")
else:
    print("WARNING: SLACK_BOT_TOKEN is not set, slack upload will be disabled")

def send_message_to_slack(message, channel="#proj-wholecellmodeling"):
    if SLACK_BOT_TOKEN is None:
        print(f"WARNING: slack report is disabled (SLACK_BOT_TOKEN not set), message: {message}")
        return
    client = WebClient(SLACK_BOT_TOKEN)
    client.chat_postMessage(
        channel=channel,
        blocks=[{
			"type": "section",
			"text": {
				"type": "mrkdwn",
				"text": ":wave: " + message
            },
        }]
    )

def send_image_to_slack(message, file_path, file_name=None, channel="#proj-wholecellmodeling"):
    if SLACK_BOT_TOKEN is None:
        print(f"WARNING: slack report is disabled (SLACK_BOT_TOKEN not set), message: {message}")
        return
    client = WebClient(SLACK_BOT_TOKEN)
    file_name = file_name or os.path.basename(file_path)
    new_file = client.files_upload_v2(
        title=file_name,
        filename=file_name,
        content=open(file_path, 'rb').read(),
    )

    file_url = new_file.get("file").get("permalink")
    client.chat_postMessage(
        channel=channel,
        text= ":wave: " + message + "\n " + file_url
    )

def log_txt_as_img(wh, xc, size=10):
    # wh a tuple of (width, height)
    # xc a list of captions to plot
    b = len(xc)
    txts = list()
    for bi in range(b):
        txt = Image.new("RGB", wh, color="white")
        draw = ImageDraw.Draw(txt)
        font = ImageFont.truetype('data/DejaVuSans.ttf', size=size)
        nc = int(40 * (wh[0] / 256))
        lines = "\n".join(xc[bi][start:start + nc] for start in range(0, len(xc[bi]), nc))

        try:
            draw.text((0, 0), lines, fill="black", font=font)
        except UnicodeEncodeError:
            print("Cant encode string for logging. Skipping.")

        txt = np.array(txt).transpose(2, 0, 1) / 127.5 - 1.0
        txts.append(txt)
    txts = np.stack(txts)
    txts = torch.tensor(txts)
    return txts


def ismap(x):
    if not isinstance(x, torch.Tensor):
        return False
    return (len(x.shape) == 4) and (x.shape[1] > 3)


def isimage(x):
    if not isinstance(x, torch.Tensor):
        return False
    return (len(x.shape) == 4) and (x.shape[1] == 3 or x.shape[1] == 1)


def exists(x):
    return x is not None


def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d


def mean_flat(tensor):
    """
    https://github.com/openai/guided-diffusion/blob/27c20a8fab9cb472df5d6bdd6c8d11c8f430b924/guided_diffusion/nn.py#L86
    Take the mean over all non-batch dimensions.
    """
    return tensor.mean(dim=list(range(1, len(tensor.shape))))


def count_params(model, verbose=False):
    total_params = sum(p.numel() for p in model.parameters())
    if verbose:
        print(f"{model.__class__.__name__} has {total_params * 1.e-6:.2f} M params.")
    return total_params


def instantiate_from_config(config):
    if not "target" in config:
        if config == '__is_first_stage__':
            return None
        elif config == "__is_unconditional__":
            return None
        raise KeyError("Expected key `target` to instantiate.")
    return get_obj_from_str(config["target"])(**config.get("params", dict()))


def get_obj_from_str(string, reload=False):
    module, cls = string.rsplit(".", 1)
    if reload:
        module_imp = importlib.import_module(module)
        importlib.reload(module_imp)
    return getattr(importlib.import_module(module, package=None), cls)


def _do_parallel_data_prefetch(func, Q, data, idx, idx_to_fn=False):
    # create dummy dataset instance

    # run prefetching
    if idx_to_fn:
        res = func(data, worker_id=idx)
    else:
        res = func(data)
    Q.put([idx, res])
    Q.put("Done")


def parallel_data_prefetch(
        func: callable, data, n_proc, target_data_type="ndarray", cpu_intensive=True, use_worker_id=False
):
    # if target_data_type not in ["ndarray", "list"]:
    #     raise ValueError(
    #         "Data, which is passed to parallel_data_prefetch has to be either of type list or ndarray."
    #     )
    if isinstance(data, np.ndarray) and target_data_type == "list":
        raise ValueError("list expected but function got ndarray.")
    elif isinstance(data, abc.Iterable):
        if isinstance(data, dict):
            print(
                f'WARNING:"data" argument passed to parallel_data_prefetch is a dict: Using only its values and disregarding keys.'
            )
            data = list(data.values())
        if target_data_type == "ndarray":
            data = np.asarray(data)
        else:
            data = list(data)
    else:
        raise TypeError(
            f"The data, that shall be processed parallel has to be either an np.ndarray or an Iterable, but is actually {type(data)}."
        )

    if cpu_intensive:
        Q = mp.Queue(1000)
        proc = mp.Process
    else:
        Q = Queue(1000)
        proc = Thread
    # spawn processes
    if target_data_type == "ndarray":
        arguments = [
            [func, Q, part, i, use_worker_id]
            for i, part in enumerate(np.array_split(data, n_proc))
        ]
    else:
        step = (
            int(len(data) / n_proc + 1)
            if len(data) % n_proc != 0
            else int(len(data) / n_proc)
        )
        arguments = [
            [func, Q, part, i, use_worker_id]
            for i, part in enumerate(
                [data[i: i + step] for i in range(0, len(data), step)]
            )
        ]
    processes = []
    for i in range(n_proc):
        p = proc(target=_do_parallel_data_prefetch, args=arguments[i])
        processes += [p]

    # start processes
    print(f"Start prefetching...")
    import time

    start = time.time()
    gather_res = [[] for _ in range(n_proc)]
    try:
        for p in processes:
            p.start()

        k = 0
        while k < n_proc:
            # get result
            res = Q.get()
            if res == "Done":
                k += 1
            else:
                gather_res[res[0]] = res[1]

    except Exception as e:
        print("Exception: ", e)
        for p in processes:
            p.terminate()

        raise e
    finally:
        for p in processes:
            p.join()
        print(f"Prefetching complete. [{time.time() - start} sec.]")

    if target_data_type == 'ndarray':
        if not isinstance(gather_res[0], np.ndarray):
            return np.concatenate([np.asarray(r) for r in gather_res], axis=0)

        # order outputs
        return np.concatenate(gather_res, axis=0)
    elif target_data_type == 'list':
        out = []
        for r in gather_res:
            out.extend(r)
        return out
    else:
        return gather_res


class MyPool:
    def __init__(self, processes, chunksize, initializer, initargs):
        assert type(processes) == int
        assert processes >= 1 or processes == -1
        if processes == -1:
            processes = None
        self.processes = processes
        if processes == 1:
            self.map_func = map
            if initializer is not None:
                initializer(*initargs)
        else:
            self.pool = Pool(processes, initializer=initializer, initargs=initargs)
            self.map_func = self.pool.imap
            if processes is not None:
                self.map_func = partial(self.map_func, chunksize=chunksize)
     
    def __enter__(self):
        if self.processes != 1:
            self.pool.__enter__()
        return self
 
    def __exit__(self, *args):
        if self.processes != 1:
            return self.pool.__exit__(*args)

# send_message_to_slack("This is a test message from Wei's difussion model")
# send_image_to_slack("Hey, this is an image generated by diffusion model", "/data/wei/stable-diffusion/logs/2022-11-06T01-34-27_hpa-ldm-vq-4-unconditional-debug/images/train/denoise_row_gs-000800_e-000013_b-000020.png")