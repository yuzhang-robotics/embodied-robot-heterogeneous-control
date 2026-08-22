#!/usr/bin/env python3
import argostranslate.package
import argostranslate.translate

def main():
    from_code = "en"
    to_code = "zh"

    print("正在更新 Argos Translate 语言包索引...")
    argostranslate.package.update_package_index()
    available_packages = argostranslate.package.get_available_packages()

    target_package = next(
        (
            package
            for package in available_packages
            if package.from_code == from_code and package.to_code == to_code
        ),
        None,
    )

    if target_package is None:
        print("没有找到 en -> zh 翻译包。")
        raise SystemExit(1)

    print(f"正在下载翻译包：{target_package}")
    package_path = target_package.download()
    argostranslate.package.install_from_path(package_path)

    print("安装完成。当前已安装语言：")
    for language in argostranslate.translate.get_installed_languages():
        print("-", language.code, language.name)


if __name__ == "__main__":
    main()
