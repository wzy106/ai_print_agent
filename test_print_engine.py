from print_engine import PrintEngine

if __name__ == "__main__":
    # 【注意】把这里的路径替换为你电脑上真实存在的一张图片路径！
    # 比如你桌面上随便找一张截图，右键 -> 复制文件地址，粘贴到这里。
    # 记得在路径前面加个 r，防止转义报错。
    test_image_path = r"D:\Rice_pests\test\images\RP00004.jpg"
    
    print(f"准备测试文件: {test_image_path}")
    
    # 初始化引擎
    engine = PrintEngine()
    
    # 执行打印流程
    result = engine.execute(test_image_path)
    
    print("\n================ 最终测试结果 ================")
    print(f"状态: {result['status']}")
    print(f"模式: {result['mode']}")
    print(f"提示: {result['msg']}")
    if result.get('path'):
        print(f"生成的PDF路径: {result['path']}")