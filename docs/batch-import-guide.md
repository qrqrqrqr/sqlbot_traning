# SQLBot Batch Import Guide

本文整理 3 类常用批量导入方式：

- 术语库批量导入
- 表注释批量导入
- SQL 示例库批量导入

以下接口路径都以 `http://<server>:8000/api/v1` 为前缀。

## 准备条件

1. 先登录 SQLBot Web，拿到当前账号的 Bearer Token，或者直接在 Swagger 页面完成 `Authorize`。
2. 导入文件只支持 `.xlsx` 和 `.xls`。
3. 术语库导入接口显式要求工作空间管理员权限；其余两个接口也建议使用有目标数据源管理权限的账号执行。

示例中的请求头统一写成：

```bash
Authorization: Bearer <your_token>
```

## 1. 术语库批量导入

后端接口位于 `backend/apps/terminology/api/terminology.py`，路由前缀是 `/system/terminology`。

### 下载模板

```bash
curl -L \
  -H "Authorization: Bearer $TOKEN" \
  -o terminology_template.xlsx \
  "http://<server>:8000/api/v1/system/terminology/template"
```

如果你想先导出现有术语再增量修改，可以使用：

```bash
curl -L \
  -H "Authorization: Bearer $TOKEN" \
  -o terminology_export.xlsx \
  "http://<server>:8000/api/v1/system/terminology/export"
```

### 模板字段

模板按前 5 列读取：

| 列 | 含义 | 填写规则 |
| --- | --- | --- |
| `word` | 主术语 | 必填 |
| `other_words` | 同义词 | 多个值用英文逗号分隔 |
| `description` | 术语解释 | 必填 |
| `datasource` | 生效数据源 | 多个数据源名称用英文逗号分隔；当 `all_data_sources=Y` 时可留空 |
| `all_data_sources` | 是否对全部数据源生效 | 支持 `Y/Yes/True`，其余值按否处理 |

注意事项：

- 当 `all_data_sources=Y` 时，系统会忽略 `datasource` 列。
- 当 `all_data_sources` 不是 `Y/Yes/True` 时，必须填写已经存在的数据源名称。
- 同一个 Excel 内会先去重，去重键基于 `word + other_words + datasource scope`。
- 批量写入成功后会统一触发术语 embedding 构建。

### 上传文件

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@terminology_template.xlsx" \
  "http://<server>:8000/api/v1/system/terminology/uploadExcel"
```

返回示例：

```json
{
  "success_count": 12,
  "failed_count": 1,
  "duplicate_count": 2,
  "original_count": 15,
  "error_excel_filename": "terminology_abc123_error.xlsx"
}
```

如果 `failed_count > 0`，可以下载失败明细：

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"file":"terminology_abc123_error.xlsx"}' \
  -o terminology_error.xlsx \
  "http://<server>:8000/api/v1/system/download-fail-info"
```

## 2. 表注释批量导入

后端接口位于 `backend/apps/datasource/api/datasource.py`，路由前缀是 `/datasource`。

这一套接口的设计是：

1. 先下载模板，或者直接导出某个数据源当前的表结构备注。
2. 在 Excel 中修改表备注和字段备注。
3. 把同一个 Excel 回传给目标数据源。

### 下载空模板

```bash
curl -L \
  -H "Authorization: Bearer $TOKEN" \
  -o datasource_schema_template.xlsx \
  "http://<server>:8000/api/v1/datasource/exportDsSchema/0"
```

### 导出某个数据源当前注释

把下面的 `<datasource_id>` 换成目标数据源 ID：

```bash
curl -L \
  -H "Authorization: Bearer $TOKEN" \
  -o datasource_schema.xlsx \
  "http://<server>:8000/api/v1/datasource/exportDsSchema/<datasource_id>"
```

### 模板结构

Excel 至少包含以下结构：

- 第一张总表必须叫 `数据表列表`
- 总表包含 3 列：`Sheet名称`、`表名`、`表备注`
- 每个明细 sheet 对应一张表，包含 2 列：`字段名`、`字段备注`

系统会按总表中的 `Sheet名称 -> 表名` 映射关系，更新对应表和字段的 `custom_comment`。

建议做法：

- 最稳妥的是先导出 `/exportDsSchema/<datasource_id>`，直接在导出的文件上改备注，再原样回传。
- 不要改动 sheet 名称和表名，否则字段备注无法正确映射到目标表。

### 上传文件

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@datasource_schema.xlsx" \
  "http://<server>:8000/api/v1/datasource/uploadDsSchema/<datasource_id>"
```

成功时直接返回：

```json
true
```

解析失败时会返回 `500 Parse Excel Failed: ...`。

## 3. SQL 示例库批量导入

后端接口位于 `backend/apps/data_training/api/data_training.py`，路由前缀是 `/system/data-training`。

### 下载模板

```bash
curl -L \
  -H "Authorization: Bearer $TOKEN" \
  -o data_training_template.xlsx \
  "http://<server>:8000/api/v1/system/data-training/template"
```

如果你想导出现有示例再编辑，可以使用：

```bash
curl -L \
  -H "Authorization: Bearer $TOKEN" \
  -o data_training_export.xlsx \
  "http://<server>:8000/api/v1/system/data-training/export"
```

### 模板字段

系统会按模板列数读取：

- 普通工作空间模板通常是 3 列：`question`、`description`、`datasource_name`
- 当当前用户所在 `oid == 1` 时，模板会多一列 `advanced_application_name`

填写规则：

| 列 | 含义 | 填写规则 |
| --- | --- | --- |
| `question` | 自然语言问题 | 必填 |
| `description` | 示例 SQL | 必填 |
| `datasource_name` | 生效数据源名称 | 可空，但不能和 `advanced_application_name` 同时为空 |
| `advanced_application_name` | 生效高级应用名称 | 仅当模板里带这一列时填写 |

注意事项：

- 数据源名称必须和当前工作空间中的数据源名称完全一致。
- 如果模板中包含 `advanced_application_name`，其值必须能匹配到同工作空间下 `type == 1` 的高级应用名称。
- 同一个 Excel 内会按 `question + datasource_name + advanced_application_name` 先去重。
- 批量写入成功后会统一触发示例 embedding 构建。

如果你是从模板下载开始操作，最稳妥的方式是严格按模板列数填写，不要自行增删列。

### 上传文件

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@data_training_template.xlsx" \
  "http://<server>:8000/api/v1/system/data-training/uploadExcel"
```

返回示例：

```json
{
  "success_count": 20,
  "failed_count": 2,
  "duplicate_count": 3,
  "original_count": 25,
  "error_excel_filename": "data_training_def456_error.xlsx"
}
```

如果有失败记录，同样通过统一接口下载错误文件：

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"file":"data_training_def456_error.xlsx"}' \
  -o data_training_error.xlsx \
  "http://<server>:8000/api/v1/system/download-fail-info"
```

## 建议流程

如果你的目标是稳定批量维护 RAG 资产，建议固定按下面的顺序操作：

1. 先导出模板或现有数据，不要手写列头。
2. 先在小样本 Excel 上验证一轮，确认数据源名称和高级应用名称完全匹配。
3. 批量上传后检查 `success_count`、`failed_count`、`duplicate_count`。
4. 如有失败，下载 `_error.xlsx` 修正后再次导入。
