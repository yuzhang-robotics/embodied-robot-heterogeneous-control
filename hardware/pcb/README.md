# Custom Base-Driver PCB

This directory contains the editable design export for the Octopus robot's base-driver board. The board connects the STM32F407 minimum-system module, Jetson UART, four motor/encoder assemblies, two TB6612FNG drivers and the two battery power paths.

> 中文简介：本目录提供“章鱼号”底层驱动板的 EasyEDA Pro 可编辑交换文件、导入方法和文件校验值。该文件已经成功回导，但不是可直接下单生产的 Gerber、BOM 与坐标文件集合。

## EasyEDA Pro source

- File: [`easyeda-pro/octopus-base-driver-v1.0.epro2`](easyeda-pro/octopus-base-driver-v1.0.epro2)
- Format: EasyEDA Pro `epro2` V3 interchange package
- Import-tested with: EasyEDA Pro V3.2.135
- SHA-256: `e70d9601b270a4069da39514ad9967569b05c352323df8cc548aded1b6ffcbae`

To open the design:

1. Start EasyEDA Pro.
2. Choose **File → Import → EasyEDA (Professional)**.
3. Select `octopus-base-driver-v1.0.epro2`.
4. Keep document, footprint and 3D-model association enabled, then import.

The exported package has been re-imported successfully. The original `.eprj2` project database, local edit history and manufacturing orders are not part of the public repository.

This is the board used for the hardware-validated thesis robot, but it is not a ready-to-order production release. Re-run ERC/DRC, inspect footprints and design rules, and generate fresh Gerber, BOM and placement files before manufacturing.

Electrical interfaces, power limitations and first-power-on precautions are documented in [`docs/hardware/README.md`](../../docs/hardware/README.md).

## License

The editable hardware design is released under [CERN-OHL-P-2.0](../LICENSE), copyright `yuzhang-robotics`. See [`hardware/NOTICE`](../NOTICE) for the scope of the hardware license.
