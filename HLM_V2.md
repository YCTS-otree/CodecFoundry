# HLM v2 规范

HLM（FlashCut Highlight Markers）是 UTF-8 编码的 JSON 文本文件，规范后缀为大写 `.HLM`。
所有时间均为原片时间轴上的秒数；生产者不得把代理文件或 FC 预导出的片段写成联动输入。

## 顶层字段

| 字段 | 类型 | 要求 |
|---|---|---|
| `format` | string | 固定为 `FlashCut Highlight Markers` |
| `format_version` | integer | 当前为 `2` |
| `flashcut_version` | string | 生成文件的 FC 版本 |
| `exported_at` | string | ISO 8601 时间 |
| `FC_MD5` | string | 原片全文件 MD5；后台计算尚未完成时可为空 |
| `source` | object | 原片身份与完整媒体参数 |
| `highlights` | array | 当前原片的完整 HL 数据，不因本次只处理部分 HL 而裁剪 |
| `processing` | object | 可选；用于 CF 联动时必须存在 |

`source` 必须包含 `path`、`filename`、`size`、`mtime_ns`、`duration`、`fps`、
`width`、`height`、`codec` 和 `format_name`。`path` 通常是绝对路径；相对路径以
HLM 所在目录为基准。

每个 `highlights[]` 项包含稳定 `id`、`time`、`note`、`created_at`、
`updated_at`，以及 `export.override/pre/post`。

## CodecFoundry 联动段

`processing` 的规范结构如下：

```json
{
  "processor": {
    "name": "CodecFoundry",
    "minimum_version": "1.2.0"
  },
  "source_mode": "original",
  "encoding": {
    "codec": "hevc",
    "container": "mp4",
    "copy_subtitles": false,
    "profile": "codecfoundry_preferences"
  },
  "output": {
    "directory": "D:\\Exports",
    "overwrite": false
  },
  "jobs": [
    {
      "id": "job-0001",
      "highlight_ids": ["hl-example"],
      "start": 8.0,
      "end": 13.0,
      "duration": 5.0,
      "output_stem": "source_HL_00-00-10.00"
    }
  ]
}
```

规则：

- `source_mode` 固定为 `original`；CF 必须直接打开 `source.path`。
- `jobs` 是本次待处理列表；`highlights` 仍是完整标记全集。
- 重叠 HL 可以合并成一个 job，此时 `highlight_ids` 包含所有来源 HL id。
- `duration` 必须等于 `end - start`；范围不得越过实际原片时长。
- `output_stem` 只能是安全的单个文件名组成部分，不得包含目录。
- `profile=codecfoundry_preferences` 表示未在 HLM 中固定的 CQ、preset、lookahead 等参数沿用
  CF 当前偏好，用户可在 CF 开始前修改。
- CF 应校验格式版本、最低应用版本、原片存在性/尺寸、HL 引用、时间窗和输出名，然后再建队。
