# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import sys
import tempfile

from argparse import Namespace
from typing import Any, Dict, List, Literal, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adadelta
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torcheval.metrics import MulticlassAccuracy
from torchtnt.framework import AutoUnit, fit, init_fit_state, State
from torchtnt.utils import copy_data_to_device, init_from_env, seed, TLRScheduler
from torchtnt.utils.loggers import TensorBoardLogger
from torchtnt.utils.timer import get_timer_summary
from torchvision import datasets, transforms

Batch = Tuple[torch.Tensor, torch.Tensor]


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)
        return output


class MyUnit(AutoUnit[Batch]):
    def __init__(
        self,
        *,
        tb_logger: TensorBoardLogger,
        train_accuracy: MulticlassAccuracy,
        lr: float,
        gamma: float,
        **kwargs: Dict[str, Any],  # kwargs to be passed to AutoUnit
    ) -> None:
        super().__init__(**kwargs)
        self.tb_logger = tb_logger
        self.lr = lr
        self.gamma = gamma

        # create an accuracy Metric to compute the accuracy of training
        self.train_accuracy = train_accuracy
        self.loss = None

    def configure_optimizers_and_lr_scheduler(
        self, module: torch.nn.Module
    ) -> Tuple[torch.optim.Optimizer, TLRScheduler]:
        optimizer = Adadelta(module.parameters(), lr=self.lr)
        lr_scheduler = StepLR(optimizer, step_size=1, gamma=self.gamma)
        return optimizer, lr_scheduler

    def compute_loss(self, state: State, data: Batch) -> Tuple[torch.Tensor, Any]:
        inputs, targets = data
        outputs = self.module(inputs)
        outputs = torch.squeeze(outputs)
        loss = torch.nn.functional.nll_loss(outputs, targets)

        return loss, outputs

    def update_metrics(
        self,
        state: State,
        data: Batch,
        loss: torch.Tensor,
        outputs: Any,
    ) -> None:
        self.loss = loss
        _, targets = data
        self.train_accuracy.update(outputs, targets)

    def log_metrics(
        self, state: State, step: int, interval: Literal["step", "epoch"]
    ) -> None:
        self.tb_logger.log("loss", self.loss, step)

        accuracy = self.train_accuracy.compute()
        self.tb_logger.log("accuracy", accuracy, step)

    def on_train_epoch_end(self, state: State) -> None:
        super().on_train_epoch_end(state)
        # reset the metric every epoch
        self.train_accuracy.reset()

    def eval_step(self, state: State, data: Batch) -> None:
        step_count = state.eval_state.progress.num_steps_completed
        data = copy_data_to_device(data, self.device)
        inputs, targets = data

        outputs = self.module(inputs)
        loss = torch.nn.functional.nll_loss(outputs, targets)
        self.tb_logger.log("evaluation loss", loss, step_count)


def main(argv: List[str]) -> None:
    # parse command line arguments
    args = get_args(argv)

    # seed the RNG for better reproducibility. see docs https://pytorch.org/docs/stable/notes/randomness.html
    seed(args.seed)

    # device and process group initialization
    device = init_from_env()

    # avoid torch autocast exception
    if device.type == "mps":
        device = torch.device("cpu")

    path = tempfile.mkdtemp()
    tb_logger = TensorBoardLogger(path)

    on_cuda = device.type == "cuda"

    transform = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
    )

    train_dataset = datasets.MNIST(
        "../data", train=True, download=True, transform=transform
    )
    eval_dataset = datasets.MNIST("../data", train=False, transform=transform)

    train_dataloader = DataLoader(
        train_dataset, batch_size=args.batch_size, pin_memory=on_cuda
    )
    eval_dataloader = DataLoader(
        eval_dataset, batch_size=args.test_batch_size, pin_memory=on_cuda
    )

    module = Net()
    train_accuracy = MulticlassAccuracy(device=device)

    my_unit = MyUnit(
        tb_logger=tb_logger,
        train_accuracy=train_accuracy,
        lr=args.lr,
        gamma=args.gamma,
        module=module,
        device=device,
        strategy="ddp",
        log_frequency_steps=args.log_frequency_steps,
        precision=args.precision,
        gradient_accumulation_steps=4,
        detect_anomaly=True,
        clip_grad_norm=1.0,
    )

    state = init_fit_state(
        train_dataloader=train_dataloader,
        eval_dataloader=eval_dataloader,
        max_epochs=args.epochs,
    )

    fit(state, my_unit)
    print(get_timer_summary(state.timer))

    if args.save_model:
        torch.save(module.state_dict(), "mnist_cnn.pt")


def get_args(argv: List[str]) -> Namespace:
    """Parse command line arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed", type=int, default=1, metavar="S", help="random seed (default: 1)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training (default: 64)",
    )
    parser.add_argument(
        "--test-batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="input batch size for testing (default: 1000)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=14,
        metavar="N",
        help="number of epochs to train (default: 14)",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=1.0,
        metavar="LR",
        help="learning rate (default: 1.0)",
    )
    parser.add_argument(
        "--gamma",
        type=float,
        default=0.7,
        metavar="M",
        help="Learning rate step gamma (default: 0.7)",
    )
    parser.add_argument(
        "--save-model",
        action="store_true",
        default=False,
        help="For Saving the current Model",
    )

    parser.add_argument(
        "--log-frequency-steps", type=int, default=10, help="log every n steps"
    )

    parser.add_argument(
        "--precision",
        type=str,
        default=None,
        help="fp16 or bf16",
        choices=["fp16", "bf16"],
    )

    parser.add_argument(
        "--eval_max_steps_per_epoch",
        type=int,
        default=20,
        help="the max number of steps to run per epoch for evaluation",
    )

    return parser.parse_args(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
