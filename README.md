# 亚洲体育官员交流资质与赛事指派

协调亚洲体育协会之间的人员资质、培训、交流名额和赛事指派。

## 参与方与事实

主要参与方包括区域体育合作秘书处、国家和地区协会、运动员、技术官员、赛事承办方。领域资料记录以下已经确认的事实：

- 亚洲重要国家体育单项组织合作项目启动
- 合作包括运动员和技术官员交流
- 活动涉及多个国家和地区组织及澳门合作协议

## 业务约束

- 人员资质版本
- 交流申请
- 赛事岗位指派
- 利益冲突
- 替补与申诉

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/news_context_003.py` 负责读取和校验这些资料。

## 人员交流与赛事指派系统

`src/exchange/` 是可运行的指派系统（纯标准库，事件溯源）：

| 模块 | 职责 |
| --- | --- |
| `model.py` | 证书、培训、语言、签证、关系、材料版本、时段与休息间隔计算 |
| `policy.py` | 资格四类检查：等级与有效期、语言签证地域名额、时段重叠与休息间隔、参赛队关系；失败条款可被秘书处逐条豁免 |
| `store.py` | 只增 JSONL 事件存储，按序号校验重放；传 `None` 为内存模式 |
| `system.py` | 协会维护材料、承办方提交岗位、秘书处提案/批准例外/发邀/裁决、答复暂停、退出与赛程释放、执裁评价、替补、申诉、持续待办 |
| 视图 | `organizer_view()` 最小披露；`explain()` 解释指派依据、例外批准人、换人保留记录 |

典型工作流：

```python
from datetime import datetime, date
from src.exchange.system import AssignmentSystem
from src.exchange.model import material, Certificate, Language, Visa

sys = AssignmentSystem()
sys.register_association("secretariat", "A-HKG", "香港协会")
sys.register_association("secretariat", "A-JPN", "日本协会")
sys.register_event("secretariat", "EV1", "A-HKG", "HK", "亚洲交流赛")
sys.register_person("assoc:A-JPN", "P1", "A-JPN", "官方-01", material(
    1, "EAST", disciplines=["fb"],
    certificates=[Certificate("C1", "fb", 3, date(2025, 1, 1), date(2027, 12, 31))],
    languages=[Language("zh", "B2"), Language("en", "C1")],
    visas=[Visa("HK", date(2027, 12, 31))],
))
sys.submit_position("assoc:A-HKG", "POS1", "EV1", "fb", 3, {"zh"},
    [("S1", datetime(2026, 11, 1, 9), datetime(2026, 11, 1, 12))],
    quotas={"EAST": 2}, participating_teams={"T-A"})
inv = sys.propose("secretariat", "POS1", "P1")
sys.issue("secretariat", inv)                 # 不合格会带条款代码抛 IneligibleError
sys.respond("person:P1", inv, "accepted")      # 重复确认幂等返回原决定；矛盾则暂停岗位
```

角色标识：秘书处 `secretariat`、协会 `assoc:<协会ID>`、本人 `person:<人员ID>`。
协会可申请例外但只有秘书处可批准；退出/赛程调整只释放未履行场次；
`issue_substitute` 安排的替补只承接剩余场次，前任评价与依据随岗位保留。
完整规则见 `docs/domain-rules.md`。

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
