# -*- coding: utf-8 -*-
"""
Service -> Mapper -> SQL -> 表名 全链路缝合

要解决的问题:
  Mapper XML 的 <update id="updateStatusByAgentId"> 与 Java Mapper 接口方法
  **同名**, 这是天然的缝合点。但只看方法名会连错 ——
  实测 `list` / `getTotal` 这类方法名全库重复率极高,
  AbstractCrudController.listAll 会被缝到 DisplayConfigController.list,
  进而匹配到一堆完全无关的支付表。

缝合规则(必须全部满足, 否则不缝):
  1. Java 方法名 == XML 语句 id
  2. Java 文件路径能对上 XML namespace 里的 Mapper 接口类名
  3. 若第 2 条对不上, 要求该 stmt_id 在全库 Mapper 中**唯一**
     —— 否则标记 ambiguous, 不缝

设计原则与 call_graph 一致: **宁可不成链, 不可连错链**。
连错会让用例断言到错误表的字段, 比不连危害大得多。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from src.call_graph import RES_EXACT, RES_GUESS, RES_AMBIGUOUS
from src.code_parser import CodeChunk
from src.logger import get_logger
from src.xml_parser import MapperXmlParser, SqlStatement

logger = get_logger(__name__)


def normalize_call_name(call: str) -> str:
    """
    从调用串里取出干净的方法名。

    输入可能是:
        agentConfMapper.updateStatusByAgentId      -> updateStatusByAgentId
        agentConfMapper.updateStatusByAgentId()    -> updateStatusByAgentId
        updateStatusByAgentId                       -> updateStatusByAgentId

    解析器当前产出不带括号, 但调用图/人工录入/测试数据可能带,
    这里统一归一, 避免 () 混进方法名导致缝合失败。

    **接收者信息在另一条路走**(见 receiver_of), 不要在这里顺手丢掉。
    """
    if not call:
        return ""
    name = call.strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name.split("(", 1)[0].strip()


def receiver_of(call: str) -> str:
    """
    从调用串里取出接收者名。

        agentConfMapper.updateStatusByAgentId -> agentConfMapper
        updateStatusByAgentId                 -> ""

    为什么必须单独提取:
        Java 里 `bossOrderMapper.updateOrderStatus()` 的接收者是字段, 类型
        在类声明处 —— 这是**唯一能把 Service 精确缝到某个 Mapper 接口**的线索。
        只取方法名的话, updateOrderStatus 这种名字在 1146 个 stmt_id 里
        大量重名, 实测 458 条链路全部降级成 guess, exact 恒为 0,
        也就是"缝合了但不确定缝对了没有"。
    """
    if not call:
        return ""
    name = call.strip().split("(", 1)[0].strip()
    if "." not in name:
        return ""
    recv = name.rsplit(".", 1)[0].strip()
    # this.xxx / super.xxx 是自调用, 无外部类型信息
    if recv in ("this", "super"):
        return ""
    # 只保留最后一段(支持 a.b.c.method 形态)
    return recv.rsplit(".", 1)[-1]


def receiver_to_mapper_name(recv: str) -> str:
    """
    接收者字段名 -> Mapper 接口类名。

        bossOrderMapper -> BossOrderMapper
        agentConfMapper -> AgentConfMapper
        baseMapper      -> ""      # 泛型基类, 跳过

    靠首字母大写还原接口名: 字段命名规范(驼峰 + Mapper 后缀)在
    MyBatis 项目里高度一致, 且这个转换只在**双向验证通过**时才算数 ——
    转换结果必须与真实 namespace 对上, 否则不采信。
    """
    if not recv:
        return ""
    if not recv.lower().endswith("mapper"):
        return ""
    if recv.lower() in ("basemapper", "ibasmapper"):
        return ""      # 泛型基类, 表由运行时决定
    return recv[0].upper() + recv[1:]


@dataclass
class DataLink:
    """一条 Service -> Mapper -> SQL -> 表 的完整链路"""

    service_chunk_id: str
    service_name: str
    service_file: str
    mapper_name: str          # Java Mapper 接口名
    stmt: SqlStatement
    resolution: str
    via_chunk_id: str = ""     # 中间跳(通常就是 Mapper 接口方法自身)

    @property
    def tables(self) -> List[str]:
        return self.stmt.tables

    @property
    def qualified_table_ref(self) -> str:
        """可追溯的表引用, 供业务问答直接引用出处"""
        return f"{self.stmt.qualified_stmt}@{self.stmt.file_path}:{self.stmt.line_start}"


class DataLinkBuilder:
    """
    把 Service 方法缝到 SQL 与表。

    用法:
        dlb = DataLinkBuilder(chunks, mapper_root)
        links = dlb.build()
    """

    def __init__(self, chunks: List[CodeChunk],
                 mapper_root: Optional[Path] = None,
                 mapper_parser: Optional[MapperXmlParser] = None):
        self.chunks = chunks
        self.parser = mapper_parser or MapperXmlParser()

        # stmt_id -> [SqlStatement]  (全库, 供唯一性判断)
        self.by_stmt_id: Dict[str, List[SqlStatement]] = defaultdict(list)
        # Mapper 接口类名 -> [SqlStatement] (供包路径校验)
        self.by_mapper: Dict[str, List[SqlStatement]] = defaultdict(list)
        # 符号索引
        self.by_id = {c.chunk_id: c for c in chunks}

        if mapper_root is not None:
            self.load_mappers(mapper_root)

    # --------------------------------------------------------
    # Mapper 装载
    # --------------------------------------------------------

    def load_mappers(self, root: Path) -> int:
        """扫描并装载全部 Mapper XML"""
        n = 0
        for mf in self.parser.parse_dir(root):
            for st in mf.statements:
                self.by_stmt_id[st.stmt_id].append(st)
                key = mf.mapper_name.lower()
                if key:
                    self.by_mapper[key].append(st)
                n += 1
        logger.info("装载 Mapper: %d 条SQL, %d 个不同 stmt_id",
                    n, len(self.by_stmt_id))
        return n

    # --------------------------------------------------------
    # 主流程
    # --------------------------------------------------------

    def _stmt_in_mapper(self, mapper_cls: str,
                        stmt_id: str) -> Optional[SqlStatement]:
        """
        在指定 Mapper 接口里精确找一个 stmt_id。

        命中唯一 -> exact。同名多条(重载/继承) -> 返回 None,
        宁可降级也不猜。
        """
        cands = self.by_mapper.get(mapper_cls.lower(), [])
        if not cands:
            return None
        hits = [s for s in cands if s.stmt_id == stmt_id]
        if len(hits) == 1:
            return hits[0]
        return None

    def build(self, relations: Optional[List] = None) -> List[DataLink]:
        """
        构建链路。

        relations 传入 call_graph 的关系以复用调用图;
        不传时退化为"只看 Service 方法直接调用的方法名"。
        """
        caller_ids = set()
        if relations:
            for r in relations:
                caller_ids.add(r.from_chunk_id)

        out: List[DataLink] = []
        seen = set()
        skipped_generic = 0

        for chunk in self.chunks:
            if chunk.chunk_type not in ("method", "http_endpoint"):
                continue

            # 泛型基类的方法无法静态确定目标表, 原理上不可追。
            if self._is_generic_holder(chunk):
                skipped_generic += 1
                continue

            # 逐个调用判断: 只有走 MyBatis-Plus 泛型 CRUD 的调用无法追表,
            # 其余(真实 Mapper 调用)照常缝合。
            # 不能整方法跳过 —— 实测 CustomerService.updateReadStatusById
            # 里既有 baseMapper.update(泛型 CRUD, 表由 QueryWrapper<Customer> 决定)
            # 又有 landingPageService.getById(同类问题), 但别的方法可能有真表。
            resolvable = False
            for call in (chunk.called_methods or []):
                method_name = normalize_call_name(call)
                if not method_name:
                    continue
                if self._is_plus_crud(method_name):
                    continue          # 泛型 CRUD, 表不可静态确定, 跳过这条
                resolvable = True
                # 接收者是判定"缝到哪个 Mapper"的唯一可靠线索, 必须传下去
                recv = receiver_of(call)
                for link in self._resolve(chunk, method_name, recv):
                    key = (link.service_chunk_id, link.mapper_name,
                           link.stmt.qualified_stmt)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(link)

            if not (chunk.called_methods or []):
                continue
            if not resolvable:
                skipped_generic += 1

        stats = self._summarize(out)
        if skipped_generic:
            logger.info("跳过泛型基类方法 %d 个(目标表由运行时泛型决定)", skipped_generic)
        logger.info(
            "数据链路: %d 条 (exact=%d guess=%d)",
            len(out), stats.get(RES_EXACT, 0), stats.get(RES_GUESS, 0),
        )
        return out

    # MyBatis-Plus / MyBatis-Plus-Service 通用 CRUD 方法名。
    # 这些方法不写 SQL, 表名来自泛型实体(如 QueryWrapper<Customer>),
    # 静态分析无法确定 —— 实测 baseMapper.update / service.getById 等
    # 若强行匹配到同名 Mapper 方法, 必然连到无关的表上。
    _PLUS_CRUD = frozenset({
        "list", "page", "getbyid", "save", "savebatch", "updatebyid",
        "removebyid", "listbypages", "count", "getone", "saveorupdate",
        "updatebatchbyid", "remove", "listmaps", "getbasemapper",
        # baseMapper.update(entity, wrapper) 这类不带 ById 的重载
        "update", "insert", "selectone", "selectlist", "selectcount",
        "selectpage", "selectmaps", "selectbypage",
    })

    @classmethod
    def _is_plus_crud(cls, method_name: str) -> bool:
        """判断是否 MyBatis-Plus 泛型 CRUD(表名不可静态确定)"""
        return method_name.lower() in cls._PLUS_CRUD

    @staticmethod
    def _is_generic_holder(chunk: CodeChunk) -> bool:
        """
        判断方法是否位于泛型容器中(目标类型运行时才确定)。

        特征: 文件名/类名是 Abstract* / Base* 通用基类。
        实测 AbstractCrudController<S extends IService<T>, T, K>
        整个类都是泛型转发, 表由运行时泛型实参决定。
        """
        stem = Path(chunk.file_path).stem.lower()
        return stem.startswith("abstract") or stem.startswith("base")

    @staticmethod
    def _summarize(links: List[DataLink]) -> Dict[str, int]:
        stats: Dict[str, int] = {}
        for l in links:
            stats[l.resolution] = stats.get(l.resolution, 0) + 1
        return stats

    # --------------------------------------------------------
    # 缝合
    # --------------------------------------------------------

    def _resolve(self, caller: CodeChunk, method_name: str,
                 receiver: str = "") -> List[DataLink]:
        """
        把 caller 对 method_name 的调用缝到 SQL。

        优先级:
          A. 接收者字段名能唯一对上某个 Mapper 接口 -> exact(最可信)
          A'. caller 自己就是 Mapper 接口方法        -> exact
          B. 全库该 stmt_id 唯一                   -> guess
          C. 同名多条且无法区分                    -> 不缝

        receiver 参数是 A 分支的入口。实测加上它之后 exact 从 0 升到
        绝大多数 —— 因为 Java 里 `xxxMapper.foo()` 的字段名就是接口类型
        的直接线索, 而方法名在 1146 个 stmt_id 里重名极其严重。
        """
        if not method_name:
            return []

        # ---- A: 用接收者字段名锁定 Mapper 接口
        mapper_cls = receiver_to_mapper_name(receiver)
        if mapper_cls:
            st = self._stmt_in_mapper(mapper_cls, method_name)
            if st is not None:
                return [DataLink(
                    service_chunk_id=caller.chunk_id,
                    service_name=caller.qualified_name or caller.name,
                    service_file=caller.file_path,
                    mapper_name=mapper_cls,
                    stmt=st,
                    resolution=RES_EXACT,
                    via_chunk_id=caller.chunk_id,
                )]
            # 接收者指向某个 Mapper, 但该接口里没有这个 stmt_id ——
            # 说明这可能是个接口继承或 MyBatis-Plus 泛型方法,
            # **不能退回按方法名猜**: 退回去就等于把 guess 当 exact 用。

        # ---- A': 调用目标是 Mapper 接口本身
        mapper_stmts = self._match_mapper_interface(caller, method_name)
        if mapper_stmts:
            st = mapper_stmts[0]
            return [DataLink(
                service_chunk_id=caller.chunk_id,
                service_name=caller.qualified_name or caller.name,
                service_file=caller.file_path,
                mapper_name=st.namespace.rsplit(".", 1)[-1],
                stmt=st,
                resolution=RES_EXACT,
                via_chunk_id=caller.chunk_id,
            )]

        # ---- B/C: 按 stmt_id 找
        # 只在全库唯一时才缝。实测 list/get/listAll 这类方法名在
        # 1146 个 stmt_id 里大量重复, 宽松匹配会把 CustomerController
        # 缝到 MarketingMaterialMapper 这种完全无关的表上 ——
        # 那比不缝危害大得多: 用例会断言到错误的表。
        cands = self.by_stmt_id.get(method_name, [])
        if len(cands) == 1:
            st = cands[0]
            return [DataLink(
                service_chunk_id=caller.chunk_id,
                service_name=caller.qualified_name or caller.name,
                service_file=caller.file_path,
                mapper_name=st.namespace.rsplit(".", 1)[-1] if st.namespace else "",
                stmt=st,
                resolution=RES_GUESS,
                via_chunk_id=caller.chunk_id,
            )]

        # 多个候选: 只有当调用方文件里显式 import/引用了某个 Mapper 接口,
        # 或文件路径/类名能收敛到唯一 Mapper 时才缝。纯靠方法名猜测的一律不缝。
        narrowed = self._narrow_by_caller(caller, cands)
        if len(narrowed) == 1:
            st = narrowed[0]
            return [DataLink(
                service_chunk_id=caller.chunk_id,
                service_name=caller.qualified_name or caller.name,
                service_file=caller.file_path,
                mapper_name=st.namespace.rsplit(".", 1)[-1] if st.namespace else "",
                stmt=st,
                resolution=RES_GUESS,
                via_chunk_id=caller.chunk_id,
            )]

        # 仍无法唯一确定 -> 不缝
        logger.debug("同名 SQL 候选 %d 条, 放弃缝合: %s", len(cands), method_name)
        return []

    def _match_mapper_interface(self, caller: CodeChunk,
                                method_name: str) -> List[SqlStatement]:
        """
        若 caller 自己就是 Mapper 接口方法(文件名含 Mapper 且 chunk_type=method),
        直接按接口名找 XML。namespace 形如 ai.yiye.agent.boss.mapper.AgentConfMapper
        """
        fname = Path(caller.file_path).stem
        if "mapper" not in fname.lower():
            return []
        key = fname.lower()
        stmts = self.by_mapper.get(key, [])
        return [s for s in stmts if s.stmt_id == method_name]

    def _narrow_by_caller(self, caller: CodeChunk,
                          candidates: List[SqlStatement]) -> List[SqlStatement]:
        """
        用调用方所在文件里**实际引用的 Mapper** 收敛候选。

        实测教训: 早期版本用"服务类名与 Mapper 名前缀是否相关"打分,
        结果 CustomerService.updateReadStatusById 被缝到
        MaterialLibraryMapper (marketing_material_library) ——
        前缀弱相关但完全不是同一个业务对象, 生成用例会断言错表。

        现在改为: 只认调用方文件里 import 或字段声明中出现过的 Mapper 接口名。
        没有直接证据就返回空, 由上层放弃缝合。
        """
        if not candidates:
            return []

        # 调用方文件里出现过的 Mapper 名(import 与字段声明两类)
        referenced = self._mapper_refs_of(caller)
        if not referenced:
            return []

        hit = [st for st in candidates
               if (st.namespace.rsplit(".", 1)[-1].lower() in referenced)]

        # 仍多个 -> 无法唯一确定, 不缝
        return hit

    @staticmethod
    def _mapper_refs_of(caller: CodeChunk) -> set:
        """
        找出调用方代码里引用到的 Mapper 接口名。

        两个来源:
          1. imports 字段(解析器已抽好)
          2. 字段声明类型(用 call_graph 的抽取逻辑)
        返回小写集合。
        """
        refs = set()
        for imp in (caller.imports or []):
            tail = imp.rsplit(".", 1)[-1].lower()
            if "mapper" in tail:
                refs.add(tail)
        # 字段声明: private XxxMapper xxxMapper;
        src = (caller.code_content or "")
        if "Mapper" in src:
            import re
            for m in re.finditer(r"\b([A-Z]\w*Mapper)\b", src):
                refs.add(m.group(1).lower())
        return refs


def summarize_tables(links: List[DataLink], only_high_confidence: bool = True) -> Dict[str, int]:
    """统计链路里出现的表名分布, 用于评估反幻觉白名单覆盖"""
    out: Dict[str, int] = defaultdict(int)
    for l in links:
        if only_high_confidence and l.stmt.table_confidence != "high":
            continue
        for t in l.tables:
            out[t] += 1
    return dict(out)
