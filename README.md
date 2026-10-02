# 亚洲体育官员交流资质与赛事指派

协调亚洲体育协会之间的人员资质、培训、交流名额和赛事指派，覆盖从协会材料维护、
赛事岗位提交、秘书处邀请核验到答复、退出、替补、申诉的完整流程。

## 参与方与事实

主要参与方包括区域体育合作秘书处、国家和地区协会、运动员、技术官员、赛事承办方。领域资料记录以下已经确认的事实：

- 亚洲重要国家体育单项组织合作项目启动
- 合作包括运动员和技术官员交流
- 活动涉及多个国家和地区组织及澳门合作协议

## 系统组成

- `src/model.py`：领域数据结构——协会材料版本、官员档案、赛事岗位、邀请、
  资格快照、例外授权、履职评价、申诉、持续待办、append-only 审计。
- `src/intake.py`：把各协会不同格式的记录归一化（如“洲际级/Level-3/L3”、
  “2026年12月31日/长期”、“英语 C1”、自由文本关系）。无法识别的条目
  作为录入问题返回，不静默丢弃。
- `src/rules.py`：八条时点资格规则——可服务项目、证书等级与有效期、语言、
  岗位重叠、休息间隔、地域/签证、参赛队利益关系、差旅名额。
- `src/secretariat.py`：秘书处流程——按当时有效材料发邀请并冻结资格依据；
  重复确认返回原决定，矛盾答复暂停岗位；退出/改期只释放未履行安排；
  评价保留原依据；例外须非本协会授权；替补链、申诉、持续待办、
  承办方最小视图与指派解释。
- `src/persistence.py`：全部状态 JSON 落盘与恢复，服务中断后审计序号连续、
  历史指派仍可解释。
- `src/news_context_003.py`：读取并校验领域上下文资料（保留原有能力）。

## 关键规则摘要

- 协会材料版本只增不改，邀请选取“当时有效”的最新版本；证书按**服务当日**状态核验。
- 协会可以修订本方材料，但**不能独自批准本方人员的例外**。
- 同一邀请重复确认返回原决定；答复内容矛盾则暂停该岗位，等待秘书处裁定。
- 退出或赛程调整只释放尚未履行的安排；已完成评价锁定原资格依据。
- 证书到期、签证期限、替补、申诉、岗位暂停均形成带历史的持续待办，
  材料续期后旧待办自动解除。
- 承办方只获得履职所需信息（姓名、履职联系、服务时段、依据版本、例外ID）。

详见 [`docs/domain-rules.md`](docs/domain-rules.md)。

## 快速示例

```python
from datetime import date, datetime
from src.secretariat import Secretariat
from src.model import Association, Official, Event, Position, ServiceWindow, CertificationLevel, LanguageLevel
from src.intake import ingest_profile

s = Secretariat(rest_hours=24)
s.register_association(Association(id="ASSN-A", name="甲协会", territory="MAC"))
s.register_official(Official(id="OFF-1", name="甲一", association_id="ASSN-A"))

# 异构格式材料直接录入
record = ingest_profile({
    "home_territory": "MAC",
    "certs": [{"sport": "Swimming", "grade": "洲际级",
               "issued": "2025年1月1日", "expiry": "2027年12月31日"}],
    "languages": ["英语 C1"],
    "visa": [{"territory": "SGP", "expires": "2027-12-31"}],
})
s.submit_revision("ASSN-A", "OFF-1", record.profile, date(2026, 1, 1))

event = Event(id="EV-1", name="测试赛", host_association_id="HOST", territory="SGP")
s.register_event(event)
s.set_travel_quota("EV-1", "ASSN-A", 1)
event.positions["P1"] = Position(
    id="P1", event_id="EV-1", discipline="Swimming", role="裁判",
    window=ServiceWindow(datetime(2026, 11, 10, 9), datetime(2026, 11, 10, 18)),
    required_level=CertificationLevel.L3,
    required_language=("en", LanguageLevel.B2), territory="SGP",
)

invitation = s.issue_invitation("OFF-1", "P1", at=datetime(2026, 9, 1))
s.respond(invitation.id, accepted=True, at=datetime(2026, 9, 2))
print(s.explain_assignment(invitation.id)["qualification_basis"])
```

`contracts/context.schema.json` 描述领域上下文结构，`fixtures/context.json` 提供不含真实身份信息的示例。

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
