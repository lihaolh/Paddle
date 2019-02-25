# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

import core
import six
import threading
from .framework import Program, Variable, program_guard
from .data_feeder import DataFeeder
import paddle.reader.decorator as decorator

__all__ = ['PyReader']


def _convert_places(places):
    if not isinstance(places, (list, tuple)):
        places = [places]

    ret = []
    for p in places:
        if not isinstance(p, core.Place):
            tmp = core.Place()
            tmp.set_place(p)
            p = tmp

        ret.append(p)
    return ret


class PyReader(Reader):
    def __init__(self, feed_list, places, capacity):
        self._tensor_reader = None
        self._thread = None

        # TODO(zjl): to support drop_last = False 
        self._drop_last = True

        self._feed_list = feed_list
        self._var_names = [v.name for v in feed_list]

        self._queues = []

        self._places = _convert_places(places)

        self._queue_capacity = capacity

        self.queue = core.init_lod_tensor_blocking_queue(core.Variable(),
                                                         self._queue_capacity)

        self._reader = core.create_py_reader(self._queue, self._var_names,
                                             self._places, self._drop_last)

    def __call__(self):
        assert self._tensor_reader is not None, \
            "Data source of PyReader has not set yet"

        class Iterator(object):
            def __init__(self, reader):
                self._reader = reader._reader
                self._reset = reader._reset

            def __iter__(self):
                return self

            def next(self):
                ret = self._reader.read_next()
                if ret:
                    return ret
                else:
                    self._reset()
                    raise StopIteration

        return Iterator(self)

    def _reset(self):
        if self._thread:
            self._reader.reset()
            self._thread.join()

        def __thread_main__():
            for tensors in self._tensor_reader():
                array = core.LoDTensorArray()
                for item in tensors:
                    if not isinstance(item, core.LoDTensor):
                        tmp = core.LoDTensor()
                        tmp.set(item, core.CPUPlace())
                        item = tmp

                    array.append(item)

                if not self.queue.push(array):
                    break

            self.queue.close()

        self._thread = threading.Thread(target=__thread_main__)
        self._thread.daemon = True
        self._thread.start()

    def set_numpy_reader(self, reader):
        assert self._tensor_reader is None, \
            "Cannot reset the data source of PyReader"
        with program_guard(Program(), Program()):
            feeder = DataFeeder(
                feed_list=self._feed_list, place=core.CPUPlace())
            paddle_reader = feeder.decorate_reader(reader, multi_devices=False)

        def __tensor_reader_impl__():
            for slots in paddle_reader():
                yield [slots[var.name] for var in self._feed_list]

        self.set_tensor_reader(__tensor_reader_impl__)

    def set_tensor_reader(self, reader):
        assert self._tensor_reader is None, \
            "Cannot reset the data source of PyReader"
        self._tensor_reader = reader
        self._reset()
