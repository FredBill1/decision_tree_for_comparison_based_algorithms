import base64
import time
import traceback
import zlib
from collections import OrderedDict, deque
from threading import Lock
from typing import Optional

import atomics
import numpy as np
import sortednp as snp

from ..cmp_algorithms.cmp_algorithms import cmp_algorithms
from ..cmp_algorithms.CmpAlgorithm import CmpAlgorithm
from ..Config import *
from .decision_tree import DecisionTreeNode, decision_tree


class NodeHolder:
    def __init__(self) -> None:
        self.lock = Lock()
        self.progress = atomics.atomic(8, atomics.INT)
        self.initialize_scheduled = atomics.atomic(1, atomics.BYTES)
        self.initialized_flag = atomics.atomic(1, atomics.BYTES)
        self.set_progress(0, 1)

    def get_and_set_initialize_scheduled(self) -> bool:
        return self.initialize_scheduled.bit_test_set(0, atomics.MemoryOrder.RELAXED)

    def get_progress(self) -> tuple[int, int]:
        x = self.progress.load(atomics.MemoryOrder.RELAXED)
        return x >> 32, x & 0xFFFFFFFF

    def set_progress(self, i: int, total: int) -> None:
        self.progress.store((i << 32) | total, atomics.MemoryOrder.RELAXED)

    def set_progress_and_yield(self, i: int, total: int) -> None:
        "yield current thread to avoid blocking http requests"
        self.set_progress(i, total)
        time.sleep(0)

    def initialize(self, cmp_algorithm_i: int, N: int) -> None:
        with self.lock:
            self.cmp_algorithm = cmp_algorithms[cmp_algorithm_i]
            self.idx_use_letter = self.cmp_algorithm.idx_use_letter(N)
            print(f"init: `{self.cmp_algorithm.name}` with {N} elements")
            self._initialize(self.cmp_algorithm, N)
            print(f"fin:  `{self.cmp_algorithm.name}` with {N} elements")
            self.initialized_flag.store(b"\x01", atomics.MemoryOrder.RELEASE)

    def wait_until_initialized(self) -> None:
        if self.initialized_flag.load(atomics.MemoryOrder.ACQUIRE) == b"\x01":
            return
        with self.lock:
            pass

    def _initialize(self, cmp_algorithm: CmpAlgorithm, N: int) -> None:
        try:
            self.nodes, self.operation_cnts, self.leaf_cnt = decision_tree(cmp_algorithm, N, self.set_progress_and_yield)
        except Exception as e:
            traceback.print_exc()
            self.initialize_scheduled.store(b"\x00", atomics.MemoryOrder.RELEASE)
            raise e


class Nodes:
    cached: OrderedDict[tuple[int, int], NodeHolder] = OrderedDict()
    cached_lock = Lock()

    def __init__(self, node_holder: NodeHolder, visiblity_state: Optional[str], validate_visiblity_state: bool) -> None:
        self.node_holder = node_holder
        if visiblity_state is not None:
            self.visiblity_state = self.decode_visiblity(visiblity_state)
            if validate_visiblity_state:
                self.visiblity_state = self._validate_visiblity_state(self.visiblity_state)

        else:
            self.visiblity_state = np.array([0], dtype=np.int32)
            self.expand_children(self.node_holder.nodes[0])

    def _validate_visiblity_state(self, tmp_visiblity_state: np.ndarray) -> np.ndarray:
        valid = [0]
        in_valid = {0}
        for node_id in tmp_visiblity_state[1:]:
            if node_id >= len(self.node_holder.nodes):
                break
            parent_id = self.node_holder.nodes[node_id].parent.id
            if parent_id in in_valid:
                valid.append(node_id)
                in_valid.add(node_id)
        return np.array(valid, dtype=np.int32)

    @classmethod
    def get_node_holder(cls, cmp_algorithm_i: int, N: int) -> NodeHolder:
        "the built-in `functools.lru_cache` would create multiple instances of NodeHolder for the same key when multithreading, so we use our own lru cache here"
        key = (cmp_algorithm_i, N)
        with cls.cached_lock:
            if key in cls.cached:
                cls.cached.move_to_end(key)
                return next(reversed(cls.cached.values()))
            if MAX_CACHED_DECISION_TREES is not None and len(cls.cached) >= MAX_CACHED_DECISION_TREES:
                cls.cached.popitem(last=False)
            ret = NodeHolder()
            cls.cached[key] = ret
            return ret

    def get_visiblity_state(self) -> str:
        return self.encode_visiblity(self.visiblity_state)

    @staticmethod
    def encode_visiblity(visiblity: np.ndarray) -> str:
        return base64.b85encode(zlib.compress(visiblity.tobytes())).decode()

    @staticmethod
    def decode_visiblity(visiblity: str) -> np.ndarray:
        return np.frombuffer(zlib.decompress(base64.b85decode(visiblity)), dtype=np.int32)

    def node_id_visiblity(self, node_id: int) -> bool:
        i = self.visiblity_state.searchsorted(node_id)
        return i < len(self.visiblity_state) and self.visiblity_state[i] == node_id

    def node_visiblity(self, node: DecisionTreeNode) -> bool:
        return self.node_id_visiblity(node.id)

    def node_has_hidden_child(self, node: DecisionTreeNode) -> bool:
        for child in (node.left, node.right):
            if child is not None and not self.node_visiblity(child):
                return True
        return False

    def node_is_leaf(self, node: DecisionTreeNode) -> bool:
        return node.left is None and node.right is None

    def expand_children(self, node: DecisionTreeNode) -> None:
        update: list[int] = []
        Q = deque([node])
        depth = 1
        while Q:
            for _ in range(len(Q)):
                node = Q.popleft()
                for child in (node.left, node.right):
                    if child is not None:
                        update.append(child.id)
                        if depth < DISPLAY_DEPTH:
                            Q.append(child)
            depth += 1
        update = np.array(update, dtype=np.int32)
        self.visiblity_state = snp.merge(self.visiblity_state, update, duplicates=snp.DROP)

    def hide_children(self, node: DecisionTreeNode) -> None:
        deletes: list[int] = []
        Q = deque([node])
        while Q:
            node = Q.popleft()
            for child in (node.left, node.right):
                if child is not None:
                    i = self.visiblity_state.searchsorted(child.id)
                    if i < len(self.visiblity_state) and self.visiblity_state[i] == child.id:
                        deletes.append(i)
                        Q.append(child)
        self.visiblity_state = np.delete(self.visiblity_state, deletes)

    def on_tap_node(self, node_id: int) -> None:
        if node_id >= len(self.node_holder.nodes) or not self.node_id_visiblity(node_id):
            return
        node = self.node_holder.nodes[node_id]
        if self.node_is_leaf(node):
            return
        if self.node_has_hidden_child(node):
            self.expand_children(node)
        else:
            self.hide_children(node)

    def expand_all(self) -> bool:
        elem: list[int] = []
        Q = deque([self.node_holder.nodes[0]])
        tot = 1
        while Q and tot < MAX_ELEMENTS:
            node = Q.popleft()
            elem.append(node.id)
            for child in (node.left, node.right):
                if tot < MAX_ELEMENTS and child is not None:
                    tot += 1
                    Q.append(child)
        self.visiblity_state = np.array(elem, dtype=np.int32)
        return tot < MAX_ELEMENTS

    def visible_elements(self, show_full_labels: bool) -> list[dict]:
        ret = []
        for node_id in self.visiblity_state:
            node: DecisionTreeNode = self.node_holder.nodes[node_id]
            classes = []
            if self.node_is_leaf(node):
                classes.append("is_leaf")
            elif self.node_has_hidden_child(node):
                classes.append("has_hidden_child")
            label = self.node_holder.cmp_algorithm.get_label(
                node, self.node_holder.idx_use_letter, LABEL_MAX_LENGTH if show_full_labels else LABEL_CROP_LENGTH
            )
            node_data = {"data": {"id": str(node.id), "label": label}, "classes": " ".join(classes)}
            ret.append(node_data)
            if node.parent is not None:
                ret.append(node.edge_data(self.node_holder.idx_use_letter))
        return ret
