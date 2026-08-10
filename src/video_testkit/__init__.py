"""SmartFire 视频测试套件。

提供 Fake Video Provider（实现共同契约 /provider/v1）与
GB28181 Device Simulator（UDP REGISTER + Digest 鉴权），
以及一套控制面（/testkit/v1）用于重置、注入场景和触发注册。
"""

__version__ = "0.1.0"
