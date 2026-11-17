# xmlnodelib — 离线 XML 节点查询库与快照对账

把一份 XML 文档**一次性导入**持久节点库（SQLite 单文件），之后仅凭节点库执行
结构化 JSON 路径 AST 查询并导出节点值——**查询时原 XML 可以已经移走或删除**。
面向大文档结构化取数：导入与查询都是流式的，内存占用与文档大小基本无关。

在此之上，库还提供**离线快照对账**：两份已导入的完整目录节点库（如昨天与今天的
商品目录）按一份声明式映射做身份匹配与字段对照，产出独立、可脱离输入库长期筛选的
对账结果库（同样是单个 SQLite 文件）。对账全程流式 + 落盘，不把目录转成 Python
字典，也不重新读取原 XML。

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

# 快照对账独立核对（45 项检查，手写身份表推导预期事件）
python3 tests/verify_recon.py

# 大文档基准（运行时生成 ≥20 万同级元素 + 大正文容器，比较翻倍前后峰值 RSS）
python3 tools/bench.py --workdir /tmp/xnlbench

# 对账基准（20 万/40 万商品目录的导入、对账、筛选峰值 RSS 对比）
python3 tools/recon_bench.py --workdir /tmp/xnlreconbench
```

Python API：

```python
import xmlnodelib
xmlnodelib.import_xml("in.xml", "lib.nldb")                 # 已存在则抛 ImportRejected
stats = xmlnodelib.run_query("lib.nldb", ast_obj, "out.jsonl",
                             page_size=100)                  # ast_obj 为 dict
# stats == {"total": <逻辑筛选后的总匹配数>, "returned": <实际写出的记录数>}

recon = xmlnodelib.reconcile_catalogs(
    "old.nldb", "new.nldb", "mapping.json", "diff.rrdb")     # 离线快照对账
# recon.event_counts / recon.total / recon.old_identity / recon.new_identity
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

## 离线快照对账（reconcile）

输入是**两份已经导入完成的完整目录节点库**（不是增量片段）加一份映射配置；输出是
一个独立的**结果库**（SQLite 单文件，`*.rrdb`），发布后即使两个输入节点库被移走
也能继续筛选事件。原 XML 在对账阶段完全不参与。

### 映射语法

映射是一个 JSON 对象，所有路径都复用查询 AST 的**相对步骤列表**语法（轴 + 节点
测试 + 可选谓词），命名空间按 **namespace URI + local name 展开名**匹配，与源 XML
实际使用的前缀无关：

```json
{
  "version": 1,
  "namespaces": {"c": "urn:catalog", "u": "urn:units"},
  "product": {
    "select": [
      {"axis": "child", "test": {"kind": "name", "qname": "c:catalog"}},
      {"axis": "child", "test": {"kind": "name", "qname": "c:product"}}
    ],
    "sku": {"name": "product.sku",
            "path": [{"axis": "attribute",
                      "test": {"kind": "name", "qname": "c:sku"}}]},
    "fields": [
      {"name": "product/name",
       "path": [{"axis": "child", "test": {"kind": "name", "qname": "c:name"}}]}
    ],
    "variants": {
      "path": [{"axis": "child", "test": {"kind": "name", "qname": "c:variant"}}],
      "code": {"name": "variant.code",
               "path": [{"axis": "attribute",
                         "test": {"kind": "name", "qname": "c:variantCode"}}]},
      "fields": [
        {"name": "variant/stock",
         "path": [{"axis": "child", "test": {"kind": "name", "qname": "c:stock"}}]}
      ]
    }
  }
}
```

- `product.select`：从文档节点出发选择商品元素的相对路径（不可为空）。
- `product.sku`：商品键字段。每个商品上该路径**必须恰好命中一个节点，且其
  string-value 非空**；缺失、为空、命中多个都整次对账失败。
- `product.fields`：需要比较的普通商品字段列表，`name` 是结果里出现的稳定字段
  路径（同一映射内不可重名），`path` 是取值路径。普通字段**最多命中一个节点**：
  命中两个即失败；命中零个是合法的"缺失"。
- `product.variants`（可选）：规格子项路径 `path`（相对商品元素）、规格键
  `code`（与商品键同样的恰好一个非空约束，但身份按所属 SKU 局部判定）以及规格
  普通字段 `fields`（同样最多一个节点）。
- 步骤列表为空（`"path": []`）表示上下文节点本身。节点测试也支持
  `{"kind":"name","uri":"...","local":"..."}` 直接给展开名。

### 缺失与空值的区别

字段有三种状态，结果事件里以 `old_state`/`new_state` 明确区分，JSONL 的
`old_value`/`new_value` 也据此取 `null` 或 `""`：

| 状态 | 含义 | JSONL 中的值 |
|---|---|---|
| `missing` | 路径一个节点都没命中（字段不存在） | `null` |
| `empty` | 命中了节点，但其 string-value 是空字符串 | `""` |
| `value` | 命中了非空节点 | 实际字符串 |

因此"元素被删掉"（value/empty → missing）与"文字被清空"（value → empty）是两种
不同的变化，都会各自产生事件。值比较使用节点库定义的 string-value，并以流式
SHA-256 先行判等，只有真正变化的字段才把旧值/新值复制进结果库。

### 身份与集合语义

- 商品身份是 **SKU**；规格身份是 **所属 SKU 与 variantCode 的组合**。两个不同
  SKU 下同样的 `variantCode="1"` 永远不会被当成同一规格。
- 规格集合按键视为**无序**：只调换 XML 中规格的排列顺序不产生任何事件。
- 属性书写顺序、命名空间前缀改写、元素格式化差异（展开名与 string-value 不变）
  都不产生伪变化。
- 任一键缺失/为空/重复，或任一普通字段命中多个节点，对账**整体失败**并汇总报告
  所有问题，不会静默跳过问题实体后发布一份看似完整的结果。

### 结果事件

| event_kind | 含义 | 携带字段 |
|---|---|---|
| `product_added` | 新库出现、旧库没有的 SKU | sku |
| `product_removed` | 旧库有、新库没有的 SKU | sku |
| `product_field_changed` | 两库共有商品的字段状态/值变化 | sku、field_path、old/new |
| `variant_added` | 某 SKU 下新增规格 | sku、variant_code |
| `variant_removed` | 某 SKU 下删除规格 | sku、variant_code |
| `variant_field_changed` | 两库共有规格的字段状态/值变化 | sku、variant_code、field_path、old/new |

事件 id 从 1 连续编号，顺序由 `(事件种类次序, SKU, variantCode, 字段路径)` 的全序
决定，与输入文档顺序、属性顺序、前缀写法、解析批次都无关；同一次对账重复运行
得到完全相同的事件序列。实体增删事件不带 old/new 字段值；字段变化事件给出字段
路径、两侧状态以及适用时的旧值、新值。

### 命令行

```bash
# 两份快照先各自正常导入（目标已存在则拒绝）
python3 -m xmlnodelib import --xml old.xml --db old.nldb
python3 -m xmlnodelib import --xml new.xml --db new.nldb

# 对账：发布独立结果库（已存在则拒绝；失败不留可识别的完整结果）
python3 -m xmlnodelib reconcile --old-db old.nldb --new-db new.nldb \
    --mapping mapping.json --result diff.rrdb

# 之后输入节点库可移走；按 SKU / 事件种类 / 字段路径筛选（均为可重复参数，
# 同类取 OR，类间取 AND），--field-prefix 做前缀匹配，--limit 限制条数
python3 -m xmlnodelib events --result diff.rrdb --sku P1 --sku P2 \
    --event product_field_changed --field product/price --out changes.jsonl
python3 -m xmlnodelib events --result diff.rrdb --field-prefix variant/   # 输出到 stdout
```

### Python API

```python
import xmlnodelib
from xmlnodelib.recon.result import EventFilter, open_result

stats = xmlnodelib.reconcile_catalogs(
    "old.nldb", "new.nldb", "mapping.json", "diff.rrdb")
stats.total, stats.event_counts           # 总数与六类事件计数
stats.old_identity, stats.new_identity    # 两侧库身份（含内容指纹）

with open_result("diff.rrdb") as reader:  # 输入节点库已不在也能打开
    reader.count(EventFilter(kinds=("variant_added",)))
    for event in reader.iter_events(EventFilter(skus=("P1",))):
        ...
    reader.export_jsonl("p1.jsonl", EventFilter(skus=("P1",)))  # 原子写文件
```

### 落盘与发布协议

- 抽取阶段把商品、规格、字段状态（缺失/空/值 + 流式哈希）和被引用的值块写入临时
  **工作库**；六类事件完全由工作库内的集合运算 SQL 生成，Python 侧不保存目录字典。
- 发布阶段把排好序的事件与**仅被事件引用的**旧值/新值块复制进新建的结果库，校验
  行数、值块数与 `integrity_check` 后，最后才写入 `status='complete'` 并原子改名。
- 坏映射、未完成节点库、重复/缺失/空键、多值字段、写入中断都会在发布前失败：临时
  文件被清理，目标路径不会出现能被 `events`/`open_result` 识别为完整的新结果；已有
  结果库永不覆盖。
- 结果库内含两份输入库的身份记录（路径、节点数、导入时间、全库内容 SHA-256
  指纹）、规范化后的映射文本与事件计数，便于事后追溯。

### 小样本与独立核对

```bash
# 48 项检查：手写 SKU/规格身份表推导预期事件（不调用产品的映射、身份匹配或差异
# 函数），结果库用裸 sqlite3 读回；含拒绝场景、缺失<->空值、单侧字段、复杂映射
# 回退、脱离输入库筛选、重跑与解析批次不变性
python3 tests/verify_recon.py

# 运行时生成 20 万（2x 时 40 万）商品目录（每个商品 2/4 个规格），验证对账/筛选
# 计数并测量翻倍前后导入、对账、查询的峰值 RSS；不提交任何大型 XML 或数据库
python3 tools/recon_bench.py --workdir /tmp/xnlreconbench
```

实测（本仓库交付环境，1x=20 万商品 / 2x=40 万商品；全部事件计数与筛选结果经断言）：

| 阶段 | 1x 峰值 RSS | 2x 峰值 RSS | RSS 比 (2x/1x) |
|---|---|---|---|
| 导入（单份快照） | 47.5–47.7 MiB | 89.2–89.8 MiB | 1.87–1.89 |
| 对账（两份库 + 落盘差异） | 55.3 MiB | 60.4 MiB | **1.09** |
| 结果库筛选查询 | 17.8–18.3 MiB | 17.8–18.8 MiB | 1.00–1.03 |

对账阶段输入规模翻倍而峰值 RSS 几乎不变（仅增 ~5 MiB），因为目录从不进入 Python
内存：抽取按有界批次进行，超阈值即把暂存行刷入磁盘工作库，事件集合差异全部在 SQLite
内完成；结果库筛选是对只含变化事件的小库做索引查询，RSS 与目录规模无关。

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
  前缀、上下文节点不存在或不是 element/document、目标已存在、非节点库文件、对账
  键缺失/重复/多值/坏映射/未完成库等）；`2` 命令行用法错误（argparse）。
- 导入先写 `<db>.tmp-<pid>`，成功提交后原子改名；失败删除临时文件——**不会留下能
  被正常入口认作成功的新库**（查询端还会校验 meta 表的 `status=complete`）。
- 对账的工作库与结果库同样是临时文件 + 原子改名，结果库只有在写入并通过校验、
  写入 `status='complete'` 后才发布；任何失败都不会留下完整结果，已有结果不覆盖。
- 查询输出同样临时文件 + 原子改名；失败不留完整结果文件，节点库以只读方式打开，
  失败前后字节不变。

## 目录结构

```
xmlnodelib/            # 库本体
  importer.py          # 流式导入（pyexpat → SQLite 临时库 → 原子改名）
  astcheck.py          # AST 整树验证与规范化（执行前完成）
  engine.py            # 查询执行（轴/谓词/并集/导出，中间结果落 scratch 库）
  store.py             # 节点库模式与只读连接
  cli.py               # 命令行入口（import/query/reconcile/events）
  recon/               # 离线快照对账
    mapping.py         # 映射配置校验与规范化
    extract.py         # 复用引擎流式能力的节点库读取（含全库内容指纹）
    reconcile.py       # 落盘抽取、SQL 集合差异、原子发布
    result.py          # 独立结果库模式、筛选与 JSONL 导出
tests/verify.py        # 独立核对程序（不调用库内任何索引/谓词执行函数）
tests/verify_recon.py  # 对账独立核对（手写身份表 + 裸 sqlite3 读结果）
tests/samples/*.xml    # 十个小样本（三组例子、嵌套同名容器、命名空间、混合内容等）
tests/recon_samples/   # 对账前后快照、不变性样本、拒绝样本与映射
tools/gen_big.py       # 运行时生成查询基准大文档与查询 AST
tools/bench.py         # 查询侧 1x/2x 规模导入与查询的峰值 RSS 对比
tools/gen_recon_bench.py  # 运行时生成对账基准双快照、映射与事实表
tools/recon_bench.py   # 对账侧 1x/2x 导入、对账、筛选的峰值 RSS 对比
examples/*.json        # 三组例子对应的查询 AST
```

## 核对与基准

- `python3 tests/verify_recon.py`：48 项检查。预期事件完全由**手写的 SKU/规格身份
  表和字段变更三元组**（纯集合/字典）推导，不调用本产品的映射、身份匹配或差异函数；
  对账经 CLI 驱动，结果库用裸 `sqlite3` 读回。覆盖：商品增删、普通字段修改、两个不同
  SKU 下相同 variantCode 的局部身份、单商品规格增删改、无序规格调换零事件、属性次序/
  命名空间前缀变化零事件、缺失<->空值<->值三态、单侧字段出现/消失、复杂多步/后代/
  嵌套映射回退路径、重复/缺失/空键与多值字段拒绝且不留结果、坏映射与未完成库拒绝、
  按 SKU/事件种类/字段路径（含前缀与 limit）筛选、输入库移走后结果库仍可筛选、重跑与
  解析批次不变性。
- `python3 tools/recon_bench.py --workdir DIR`：运行时生成 20 万（2x 时 40 万）商品、
  每商品 2（2x 时 4）个规格的双快照，先按手写事实表断言六类事件计数，再断言四类筛选
  查询的精确条数，并比较导入/对账/筛选的峰值 RSS 翻倍比。大型材料全部运行时生成，不
  提交巨型 XML、节点库或结果库；`--keep` 可保留现场。
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
