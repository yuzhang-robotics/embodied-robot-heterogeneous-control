# PCB Design

本目录保存章鱼号机器人自制底层驱动板的可编辑设计资料。

## EasyEDA Pro 工程

- 文件：[`easyeda-pro/octopus-base-driver-v1.0.epro2`](easyeda-pro/octopus-base-driver-v1.0.epro2)
- 格式：嘉立创 EDA 专业版 `epro2` V3
- 验证版本：嘉立创 EDA 专业版 V3.2.135
- SHA-256：`e70d9601b270a4069da39514ad9967569b05c352323df8cc548aded1b6ffcbae`

导入方法：

1. 打开嘉立创 EDA 专业版。
2. 选择“文件 → 导入 → 嘉立创EDA（专业版）”。
3. 选择 `octopus-base-driver-v1.0.epro2`。
4. 保持“导入文档”“自动关联封装”和“自动关联3D模型”，然后执行导入。

该文件已经过账号信息、个人信息、绝对路径、项目成员和编辑历史扫描，并完成原理图与 PCB
重新导入测试。原始 `.eprj2` 项目数据库、历史备份和采购订单不包含在仓库中。

硬件组成、供电边界和安全注意事项见
[`docs/hardware/README.md`](../../docs/hardware/README.md)。当前工程对应已完成实物验证的
本科毕业设计基线；在直接制造或修改前，请自行执行 ERC/DRC 并复核元件库存、封装和设计规则。

## License

本目录中的可编辑硬件设计源文件采用
[CERN-OHL-P-2.0](../LICENSE)，版权归 `yuzhang-robotics` 所有；适用声明见
[`hardware/NOTICE`](../NOTICE)。
