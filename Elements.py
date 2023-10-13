from collections.abc import Callable
from threading import Lock
from typing import Optional

from Config import *
from decision_tree import DecisionTreeNode, decision_tree
from sorting_algorithms import sorting_algorithms


class ElementNode:
    def __init__(self, node_data: dict, edge_data: Optional[dict]) -> None:
        self.node_data = node_data
        self.edge_data = edge_data
        self.left: Optional[ElementNode] = None
        self.right: Optional[ElementNode] = None
        self.crop_label()

    @property
    def visible(self) -> bool:
        return self.node_data["data"]["visibility"] == "visible"

    @visible.setter
    def visible(self, value: bool) -> None:
        visible = "visible" if value else "hidden"
        self.node_data["data"]["visibility"] = visible
        if self.edge_data is not None:
            self.edge_data["data"]["visibility"] = visible

    def set_child_visible(self) -> None:
        def dfs(node: ElementNode, depth: int) -> None:
            if depth >= DISPLAY_DEPTH:
                return
            node.visible = True
            for child in (node.left, node.right):
                if child is not None:
                    dfs(child, depth + 1)

        dfs(self, 0)

    def set_child_hidden(self) -> None:
        def dfs(node: ElementNode) -> None:
            for child in (node.left, node.right):
                if child is not None and child.node_data["data"]["visibility"] != "hidden":
                    child.visible = False
                    dfs(child)

        dfs(self)

    def update_classes(self) -> None:
        if self.is_leaf():
            self.node_data["classes"] = "is_leaf"
        elif self.has_hidden_child():
            self.node_data["classes"] = "has_hidden_child"
        else:
            self.node_data["classes"] = ""

    def crop_label(self) -> None:
        label = self.node_data["data"]["full_label"]
        if len(label) > LABEL_MAX_LENGTH:
            self.node_data["data"]["croped_label"] = label[: LABEL_MAX_LENGTH - 3] + "..."
        else:
            self.node_data["data"]["croped_label"] = label

    def has_hidden_child(self) -> bool:
        return (self.left is not None and not self.left.visible) or (self.right is not None and not self.right.visible)

    def is_leaf(self) -> bool:
        return self.left is None and self.right is None

    __slots__ = ["node_data", "edge_data", "left", "right"]


class Elements:
    cached: dict[tuple[int, int], "Elements"] = {}
    cached_lock = Lock()

    def __init__(self, sorting_func: Callable[[list], None], N: int) -> None:
        DecisionTreeNode.reset_id()
        self.elements: list[dict] = []
        self.element_nodes: list[ElementNode] = []

        root, self.operation_cnts = decision_tree(sorting_func, N)

        def dfs(
            node: DecisionTreeNode,
            parent: Optional[ElementNode] = None,
            edge_data: Optional[dict] = None,
            is_left: bool = False,
            depth: int = 0,
        ) -> None:
            while len(self.element_nodes) <= node.id:
                self.element_nodes.append(None)
            self.element_nodes[node.id] = element_node = ElementNode(
                {
                    "data": {
                        "id": str(node.id),
                        "visibility": "visible" if depth < DISPLAY_DEPTH else "hidden",
                        "full_label": node.get_arr() + " " + node.get_actuals(),
                    }
                },
                edge_data,
            )
            if parent is not None:
                if is_left:
                    parent.left = element_node
                else:
                    parent.right = element_node
            self.elements.append(element_node.node_data)

            if node.cmp_xy is None:
                return
            x, y = [chr(ord("a") + x) for x in node.cmp_xy]
            for op, child in zip("<>", [node.left, node.right]):
                if child is not None:
                    is_left = op == "<"
                    edge_data = {
                        "data": {
                            "source": str(node.id),
                            "target": str(child.id),
                            "visibility": "visible" if depth + 1 < DISPLAY_DEPTH else "hidden",
                            "cmp_op": f"{x}{op}{y}",
                        }
                    }
                    self.elements.append(edge_data)
                    dfs(child, element_node, edge_data, is_left, depth + 1)

        dfs(root)

    def reset(self) -> None:
        self.element_nodes[0].set_child_hidden()
        self.element_nodes[0].set_child_visible()

    @classmethod
    def get(cls, sorting_algorithm_i: int, N: int) -> "Elements":
        key = (sorting_algorithm_i, N)
        with cls.cached_lock:
            if key not in cls.cached:
                cls.cached[key] = cls(sorting_algorithms[sorting_algorithm_i][1], N)
            return cls.cached[key]

    def get_visiblity(self) -> str:
        return "".join("1" if element["data"]["visibility"] == "visible" else "0" for element in self.elements)

    def set_visiblity(self, visiblity: str) -> None:
        for element, vis in zip(self.elements, visiblity):
            element["data"]["visibility"] = "visible" if vis == "1" else "hidden"

    def visible_elements(self) -> list[dict]:
        for element_node in self.element_nodes:
            element_node.update_classes()
        return [element for element in self.elements if element["data"]["visibility"] == "visible"]