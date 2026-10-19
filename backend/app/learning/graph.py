"""前提知識依存グラフ（卒業論文 3.3節）

単元を構成する前提知識をノード，依存関係を辺とするグラフ。生徒によらず単元ごとに固定で，
学習者プロファイルと解説生成の双方が同じノード名（id）を参照する。
辺は単元の構造を示すためのもので，解説生成の判断（Chain 2）や定着度の更新には用いない（判断はノード単位）。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

_DATA_PATH = os.path.join(os.path.dirname(__file__), 'data', 'prerequisite_graph.json')


class GraphError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_graph() -> Dict[str, Any]:
    with open(_DATA_PATH, encoding='utf-8') as f:
        graph = json.load(f)
    _validate(graph)
    return graph


def _validate(graph: Dict[str, Any]) -> None:
    seen: Dict[str, str] = {}
    for unit_id, unit in graph['units'].items():
        ids = [n['id'] for n in unit['nodes']]
        for node_id in ids:
            if node_id in seen:
                raise GraphError(f'ノード {node_id} が {seen[node_id]} と {unit_id} で重複しています')
            seen[node_id] = unit_id
        for edge in unit.get('edges', []):
            if edge['from'] not in ids or edge['to'] not in ids:
                raise GraphError(f'{unit_id} の辺 {edge} が未定義のノードを参照しています')
        _check_acyclic(unit_id, ids, unit.get('edges', []))


def _check_acyclic(unit_id: str, ids: List[str], edges: List[Dict[str, str]]) -> None:
    children: Dict[str, List[str]] = {i: [] for i in ids}
    for edge in edges:
        children[edge['from']].append(edge['to'])
    state: Dict[str, int] = {}

    def visit(node: str) -> None:
        if state.get(node) == 1:
            raise GraphError(f'{unit_id} の依存グラフに循環があります（{node}）')
        if state.get(node) == 2:
            return
        state[node] = 1
        for child in children[node]:
            visit(child)
        state[node] = 2

    for node in ids:
        visit(node)


def unit_ids() -> List[str]:
    return list(load_graph()['units'].keys())


def get_unit(unit_id: str) -> Dict[str, Any]:
    units = load_graph()['units']
    if unit_id not in units:
        raise GraphError(f'未知の単元です: {unit_id}')
    return units[unit_id]


def node_ids(unit_id: Optional[str] = None) -> List[str]:
    units = [get_unit(unit_id)] if unit_id else load_graph()['units'].values()
    return [n['id'] for unit in units for n in unit['nodes']]


def node_labels() -> Dict[str, str]:
    return {n['id']: n['label'] for unit in load_graph()['units'].values() for n in unit['nodes']}


def unit_of_node(node_id: str) -> Optional[str]:
    for unit_id, unit in load_graph()['units'].items():
        if any(n['id'] == node_id for n in unit['nodes']):
            return unit_id
    return None


def prerequisites(node_id: str) -> List[str]:
    """node_id の直接の前提ノード（参照用。辺は要確認の仮置きで，判断には用いない）。"""
    unit_id = unit_of_node(node_id)
    if unit_id is None:
        return []
    return [e['from'] for e in get_unit(unit_id).get('edges', []) if e['to'] == node_id]


def validate_node_ids(ids: List[str], unit_id: Optional[str] = None) -> List[str]:
    """未知のノード名を返す（空なら全て有効）。"""
    known = set(node_ids(unit_id))
    return [i for i in ids if i not in known]
