"""
Cookie 格式转换工具
将 EditThisCookie 导出的完整 JSON 转换为 {name: value} 字典格式
"""
import json
import sys
from pathlib import Path


def convert_editthiscookie_to_dict(input_file: str, output_file: str = None):
    """
    将 EditThisCookie 导出的 JSON 转换为 {name: value} 格式

    Args:
        input_file: EditThisCookie 导出的 JSON 文件路径
        output_file: 输出文件路径（可选，默认为输入文件名_dict.json）
    """
    # 读取输入文件
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 判断格式
    if isinstance(data, list):
        # EditThisCookie 完整格式
        result = {}
        for cookie in data:
            name = cookie.get('name', '')
            value = cookie.get('value', '')
            if name:
                result[name] = value
        print(f"已转换 {len(result)} 个 cookie")
    elif isinstance(data, dict):
        # 已经是字典格式
        result = data
        print(f"输入已是字典格式，共 {len(result)} 个 cookie")
    else:
        print("错误：无效的 JSON 格式")
        return None

    # 生成输出文件名
    if output_file is None:
        input_path = Path(input_file)
        output_file = str(input_path.parent / f"{input_path.stem}_dict.json")

    # 保存结果
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"已保存到: {output_file}")
    return result


def main():
    if len(sys.argv) < 2:
        print("用法: python convert_cookies.py <input.json> [output.json]")
        print("示例: python convert_cookies.py cookies.json cookies_dict.json")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not Path(input_file).exists():
        print(f"错误：文件不存在 - {input_file}")
        sys.exit(1)

    result = convert_editthiscookie_to_dict(input_file, output_file)
    if result:
        print("\n转换结果:")
        for name, value in result.items():
            print(f"  {name}: {value[:20]}..." if len(value) > 20 else f"  {name}: {value}")


if __name__ == "__main__":
    main()
