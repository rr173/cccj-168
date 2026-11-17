# xmlnodelib — 离线 XML 节点查询库

把一份 XML 文档**一次性导入**持久节点库（SQLite 单文件），之后仅凭节点库执行
结构化 JSON 路径 AST 查询并导出节点值——**查询时原 XML 可以已经移走或删除**。
面向大文档结构化取数：导入与查询都是流式的，内存占用与文档大小基本无关。

**明确不做的事**：不执行 XSLT、不修订原文档、不连接网络、不实现 DTD（DOCTYPE、
外部实体一律拒绝）、不做增量同步、不做多用户服务、不做通用 XPath 兼容层
（只实现下文列出的子集，且查询输入是 JSON AST，不解析 XPath 字符串）。

运行要求：Python ≥ 3.8，仅标准库（`sqlite3`、`xml.parsers.expat`）。无第三方依赖。

## 快速开始

```bash
cd /workspace

# 导入（目标库已存在时拒绝覆盖）
python3 -m xmlnodelib import --xml tests/samples/groups.xml --db /tmp/groups.nldb

# 查询（原 XML 可移走；输出文件已存在时拒绝覆盖）
mv tests/samples/groups.xml /tmp/groups.xml.moved   # 演示：查询不需要原 XML
python3 -m xmlnodelib query --db /tmp/groups.nldb \
    --ast examples/groups_filter_then_last.json --out /tmp/out.jsonl
cat /tmp/out.jsonl

# 以指定 element 为上下文查询（context.id 为库中节点 id）
python3 -m xmlnodelib query --db /tmp/groups.nldb \
    --ast examples/groups_from_c_nearest_x.json --out /tmp/out2.jsonl

# 独立核对程序（336 项检查，含参考模型对照与拒绝场景）
python3 tests/verify.py

# 大文档基准（运行时生成 ≥20 万同级元素 + 大正文容器，比较翻倍前后峰值 RSS）
python3 tools/bench.py --workdir /tmp/xnlbench
```

Python API：

```python
import xmlnodelib
xmlnodelib.import_xml("in.xml", "lib.nldb")                 # 已存在则抛 ImportRejected
stats = xmlnodelib.run_query("lib.nldb", ast_obj, "out.jsonl",
                             page_size=100)                  # ast_obj 为 dict
# stats == {"total": <逻辑筛选后的总匹配数>, "returned": <实际写出的记录数>}
```

## 节点库模型

节点种类：`document`、`element`、`attribute`、`text`、`comment`、`pi`
（processing-instruction）。每个节点有稳定整数 id，id 即文档顺序，并遵循本工具的
**属性次序约定**：同一元素的属性按展开名固定排序（无命名空间在前，然后按
namespace URI、local name 字典序），统一排在该元素之后、其孩子之前；其余节点遵循
文档顺序。该约定是本工具为稳定导出增加的次序约定，不影响其余节点的文档序。

- document 节点 id 为 1，是根元素及 prolog/epilog 中注释、PI 的父节点。
- 属性的 parent 为所属元素；属性没有兄弟（following/preceding-sibling 恒为空）。
- **text 节点**包括元素开头文字、子元素后的尾文字以及纯空白；连续字符回调与相邻
  CDATA/普通文字合并为同一个逻辑 text 节点（解析器分块不改变查询结果）；
  comment 或 PI 会把两段 text 分开。prolog/epilog 的空白不属于数据模型。
- **string-value**：element/document 为全部后代 text 按文档顺序连接（不含注释/PI
  的内容）；attribute/text/comment/pi 为各自的解析后文字。实体转义与字符引用正常
  解释，换行按 XML 解析语义处理（CRLF/CR → LF），属性值按 CDATA 类型规范化。
- PI 节点的名字为其 target（`{"uri": null, "local": target}`），值为 PI 数据。
- 拒绝 DOCTYPE（含内部子集）、外部实体与网络引用；未定义实体导致导入失败。

## 查询 AST 格式

查询输入是 JSON 对象（结构化 AST），不解析 XPath 字符串：

```json
{
  "version": 1,                       // 可选，目前仅支持 1
  "comment": "可选说明字符串",
  "namespaces": {"p": "urn:u"},        // 可选，前缀映射；xml 前缀预绑定，xmlns 不可用
  "context": {"id": 10},               // 可选；缺省为 document 节点，
                                       // 否则必须是库中已存在的 element（或 document）节点 id
  "paths": [ {"steps": [ 步骤, ... ]}, ... ]   // 非空；多条路径取并集
}
```

步骤：`{"axis": 轴, "test": 节点测试, "predicates": [谓词, ...]}`（`predicates` 可省）。
`steps` 为空列表时结果就是上下文节点本身。

**轴**（7 个）：`child`、`descendant`、`parent`、`ancestor`、`following-sibling`、
`preceding-sibling`、`attribute`。

**节点测试**：

| AST | 含义 |
|---|---|
| `{"kind":"name","qname":"p:item"}` | 按展开名精确匹配；前缀须在 `namespaces` 中绑定；**无前缀 = 无命名空间**（与 XPath 1.0 一致） |
| `{"kind":"name","uri":"urn:u","local":"item"}` | 直接给展开名（`uri` 可为 `null`） |
| `{"kind":"star"}` | `*`：匹配该轴主类型的任意节点（attribute 轴为属性，其余轴为元素） |
| `{"kind":"text"}` | `text()`：匹配 text 节点 |
| `{"kind":"node"}` | `node()`：匹配该轴上任意节点 |

**轴与节点测试的组合规则**（不适用的组合结果为空，不报错）：

- `attribute` 轴只在 element 上下文上非空；其上 `text()` 恒为空，`name`/`*` 匹配属性。
- `child`/`descendant` 轴只含 element、text、comment、pi（**不含属性**）；
  非 document/element 上下文（如 text、attribute）上为空。
- `parent`/`ancestor` 轴上只有 document 与 element；`text()` 恒为空；
  `*` 只匹配 element（document 只能用 `node()` 或显式路径到达）。
- 兄弟轴在 attribute 或 document 上下文上为空。
- 不支持 `comment()`、`processing-instruction()`、`namespace` 轴、`self`、
  `descendant-or-self`、`following`/`preceding` 轴、`前缀:*` 通配。

**谓词**（按列表顺序依次作用于当前序列）：

| AST | XPath 1.0 对应 |
|---|---|
| `{"kind":"pos","n":3}` | `[position()=3]`（位置从 1 开始） |
| `{"kind":"pos","last":true}` | `[position()=last()]` |
| `{"kind":"attr","qname":"code","value":"x"}` | `[@code='x']`（属性值等于字符串） |
| `{"kind":"str","value":"..."}` | `[string(.)='...']`（string-value 等于字符串） |
| `{"kind":"exists","path":{"steps":[...]}}` | `[相对路径]` 存在性；相对路径可用同样的轴/测试/谓词，**但不含路径并集** |
| `{"kind":"and","args":[...]}` / `{"kind":"or","args":[...]}` / `{"kind":"not","arg":...}` | 布尔组合（`args` 非空） |

## 语义规则（与 XPath 1.0 的对应关系）

1. **每上下文独立序列**：每一步对每个输入上下文节点独立地按轴方向产生候选
   （前向轴为文档顺序，反向轴 ancestor、preceding-sibling 为逆文档顺序、就近在前），
   再依次经谓词列表筛选；每个 `pos`/`last` 都基于**上一个谓词留下的、该上下文的**
   序列。绝不可能"先把所有父项的孩子合并后再取第一个"。
2. **步骤间合并**：一步结束后，各上下文的幸存节点按**节点身份**去重并恢复**文档
   顺序**，作为下一步的上下文序列。两条路径找到同一节点只保留一个；两个文字相同
   的不同节点绝不合并。
3. **反向轴位置**：`ancestor` 与 `preceding-sibling` 是反向轴，`[1]`（即
   `{"kind":"pos","n":1}`）取**最近者**；合并多个上下文的结果后恢复文档顺序供下一步
   使用。因此"先筛属性再求最近"与"最近项再判断属性"是不同的查询（见示例）。
4. **多路径并集**：`paths` 中多条路径各自独立求值，结果按节点身份去重后按文档顺序
   返回。
5. **布尔只组合真假**：`and`/`or`/`not` 的操作数只能是布尔谓词（attr/str/exists/
   and/or/not）。本引擎没有任何"整数结果"表达式，因此不存在把整数当隐式位置谓词的
   规则；`pos` 只允许直接出现在步骤的谓词列表里，不允许嵌进布尔表达式。
6. **相对路径存在性**：`exists` 只判断**当前候选节点**上相对路径的结果是否非空，
   与邻居节点的匹配互不相干；相对路径内可继续使用全部轴与谓词（含嵌套 exists），
   但不支持路径并集、任意算术或外部函数。
7. **命名空间**：节点按展开名（URI + local）匹配；查询里的前缀映射与原文写法无关，
   前缀不同不影响匹配。原文默认命名空间只作用于元素，不作用于无前缀属性（因此
   无前缀属性用 `{"kind":"name","qname":"code"}` 即无命名空间匹配）。命名空间换绑后
   各节点保留各自展开名，不做 local name 模糊匹配。`xml` 前缀预绑定到
   `http://www.w3.org/XML/1998/namespace`；`xmlns` 前缀不可用。
8. **string-value 等值**：`str` 谓词比较的是节点的 string-value 整体（element 为全部
   后代 text 的连接），比较分块流式进行，不会把超大父元素的正文常驻内存。
9. **查询前整树验证**：未知轴/测试/谓词、未绑定前缀、非法位置（<1）、exists 中出现
   并集、未知键等都会在执行前报错——不会执行一半再忽略其余条件。
10. **总匹配数**：`--page-size` 只限制写出的记录条数；节点选择与总匹配数
    （summary 行的 `total`）在逻辑筛选完成后确定，不会先截候选再计算 last。

### 三组例子（tests/samples/groups.xml）

```xml
<r><g><item code="x">A</item><item code="y">B</item><item code="x">C</item></g><g><item code="x">D</item></g><g><item code="x">E</item><item code="y">F</item></g></r>
```

- 先筛 `code=x` 再取 `last()`（`examples/groups_filter_then_last.json`）→ **C、D、E**
  （id 10、14、18）。
- 先取 `last()` 再筛 `code=x`（`examples/groups_last_then_filter.json`）→ **C、D**
  （第三组末项 F 是 code=y，被筛掉）。第三组的存在使两种顺序结果确实不同。
- 从 C（id 10）出发 `preceding-sibling::item[1]` → **B**（id 7）；
  加 `code=x` 筛选再取 `[1]`（`examples/groups_from_c_nearest_x.json`）→ **A**（id 4）。

## 输出格式（JSONL）

每行一个 JSON 对象；最后一行为汇总：

```json
{"id":10,"kind":"element","name":{"uri":null,"local":"item"},"parent":3,"value":"C"}
{"summary":true,"total":3,"returned":3,"page_size":null}
```

- `name` 仅对 element/attribute/pi 非空（pi 的 local 为 target）；document/text/
  comment 为 `null`。`parent` 对 document 为 `null`，属性的 parent 为所属元素。
- `value` 即该节点的 string-value，与节点身份对应（text 节点是自身文字，不是
  父元素的整棵子树文字；element 是全部后代 text 的连接）。超大值分块流式写出。
- 输出文件已存在时拒绝执行；写出使用临时文件 + 原子改名，失败不会留下看似完整
  的结果文件。

## 资源参数（均有默认值，CLI 与 API 同名）

| 参数 | 默认 | 作用 |
|---|---|---|
| `--max-depth` | 256 | 导入时元素最大嵌套深度，超限报错 |
| `--input-chunk` | 65536 | 导入时每次喂给解析器的字节数（改变它不影响结果，见核对程序） |
| `--value-chunk` | 262144 | 单个值分块存储的字符数（仅影响存储布局） |
| `--batch` | 5000 / 2000 | 导入/查询的 SQLite 批量写行数 |
| `--cache-kb` | 8192 | SQLite 页缓存上限（KiB） |
| `--page-size` | 无限制 | 查询最多写出的记录数（不影响 total） |
| `--scratch-dir` | 系统临时目录 | 查询中间结果落盘的 scratch 库位置 |

实现要点：导入逐事件落盘（文本按块直接写库，超大 text 不进 Python 内存）；查询的
中间节点集合写入独立的 scratch SQLite 库（节点库本身只读打开），候选序列是游标流，
`position()=n` 取到即停、`last()` 只保留最近一个，巨型兄弟列表绝不转成 Python 数组；
字符串相等分块比较；排序类临时数据落盘（`temp_store=FILE`）。不会重新读原 XML，
不会 fetchall 全部节点。此外查询 AST 规模限制为 10000 个节点、exists 嵌套 ≤ 64 层。

## 退出码与失败语义

- `0` 成功；`1` 预期内失败（XML 不合法/DOCTYPE/外部实体、超深、AST 非法、未绑定
  前缀、上下文节点不存在或不是 element/document、目标已存在、非节点库文件等）；
  `2` 命令行用法错误（argparse）。
- 导入先写 `<db>.tmp-<pid>`，成功提交后原子改名；失败删除临时文件——**不会留下能
  被正常入口认作成功的新库**（查询端还会校验 meta 表的 `status=complete`）。
- 查询输出同样临时文件 + 原子改名；失败不留完整结果文件，节点库以只读方式打开，
  失败前后字节不变。

## 目录结构

```
xmlnodelib/            # 库本体
  importer.py          # 流式导入（pyexpat → SQLite 临时库 → 原子改名）
  astcheck.py          # AST 整树验证与规范化（执行前完成）
  engine.py            # 查询执行（轴/谓词/并集/导出，中间结果落 scratch 库）
  store.py             # 节点库模式与只读连接
  cli.py               # 命令行入口
tests/verify.py        # 独立核对程序（不调用库内任何索引/谓词执行函数）
tests/samples/*.xml    # 十个小样本（三组例子、嵌套同名容器、命名空间、混合内容等）
tools/gen_big.py       # 运行时生成大文档与查询 AST
tools/bench.py         # 1x/2x 规模导入与查询的峰值 RSS 对比
examples/*.json        # 三组例子对应的查询 AST
```

## 核对与基准

- `python3 tests/verify.py`：336 项检查。每个样本先做全库导出，与基于 minidom 的
  **独立树遍历参考模型**逐节点对照（id、种类、展开名、父 id、值；属性排序按本题约定
  另核对）；所有查询用例既对照显式列出的预期节点身份/顺序/值，又对照参考模型。
  另验证：改变解析输入块与值块大小后节点序列、身份、值一致；坏尾部、DOCTYPE（含
  外部 DTD）、未定义实体、超深、未知前缀及十余种非法 AST 均被拒绝且原库字节不变；
  覆盖写拒绝；`--page-size` 不改变 total。
- `python3 tools/bench.py --workdir DIR`：运行时生成 20 万（2x 时 40 万）同级
  `<item>` 元素加一个 8MiB（2x 时 16MiB）大正文容器，测量导入与三类查询（反向轴
  最近节点、筛选后 last、父元素 string-value 等值）的峰值 RSS 并比较翻倍比。实测
  （本仓库交付环境）：导入峰值 34.0 → 41.8 MiB，各查询阶段峰值 22.6–31.6 MiB（1x）
  与 22.7–48.8 MiB（2x），RSS 比 1.0–1.54，全部查询结果经正确性断言。大型材料
  运行时生成，不提交巨型 XML、数据库或结果；`--keep` 可保留现场。

## 支持范围与限制

- 仅支持上文列出的轴、节点测试与谓词；不支持 XPath 字符串解析、算术、除
  position()/last() 外的函数、namespace 轴、变量、XSLT。
- 节点库为单文件 SQLite，查询只读；不提供并发写、增量同步或网络服务。
- 文档编码由 XML 声明决定（expat 处理）；不实现 DTD，属性无默认值、无类型化
  规范化（均按 CDATA 处理）。
