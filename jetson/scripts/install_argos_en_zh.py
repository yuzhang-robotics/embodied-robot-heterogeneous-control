#!/usr/bin/env python3
import argostranslate.package
import argostranslate.translate

from_code = "en"
to_code = "zh"

print("正在更新 Argos Translate 语言包索引...")
argostranslate.package.update_package_index()

available_packages = argostranslate.package.get_available_packages()

target_package = None
for package in available_packages:
    if package.from_code == from_code and package.to_code == to_code:
        target_package = package
        break

if target_package is None:
    print("没有找到 en -> zh 翻译包。")
    print("可用包示例：")
    for p in available_packages[:30]:
        print(p.from_code, "->", p.to_code)
    raise SystemExit(1)

print(f"找到翻译包：{target_package}")
print("正在下载...")
package_path = target_package.download()

print(f"下载完成：{package_path}")
print("正在安装...")
argostranslate.package.install_from_path(package_path)

print("安装完成。")

installed_languages = argostranslate.translate.get_installed_languages()
print("当前已安装语言：")
for lang in installed_languages:
    print("-", lang.code, lang.name)
