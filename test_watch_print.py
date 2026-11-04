import pytest
from watch_print import parse_filename_intent, validate_intent, extract_json_object


# 1. 测试文件名正则解析 (Unit Test)
def test_parse_filename_intent_basic():
    """测试：普通文件名带明确参数，能不能准确抓出来"""
    intent = parse_filename_intent("测试文档_3份_彩印_双面.pdf")
    assert intent is not None
    assert intent.copies == 3
    assert intent.color == "color"
    assert intent.sides == "double"


def test_parse_filename_intent_no_keyword():
    """测试：文件名没关键词，应该返回 None"""
    intent = parse_filename_intent("普通风景照.jpg")
    assert intent is None


def test_parse_filename_intent_ignore():
    """测试：文件名包含‘不打印’"""
    intent = parse_filename_intent("机密文件_不打印.docx")
    assert intent is not None
    assert intent.need_print is False


# 2. 测试模型返回的 JSON 提取与严格校验 (Unit Test)
def test_extract_json_object():
    """测试：模型像往常一样输出了代码块包裹的 JSON，能不能把它剥离出来"""
    # 用字符串乘法动态生成三个反引号，防止 Markdown 渲染器截断代码
    bt = "`" * 3
    raw_model_output = f"""
    好的，我已经理解了您的需求：
    {bt}json
    {{"need_print": true, "copies": 2, "color": "color", "sides": "double"}}
    {bt}
    """
    data = extract_json_object(raw_model_output)
    assert data is not None
    assert data["copies"] == 2


def test_validate_intent_success():
    """测试：正常的参数，能不能通过校验"""
    data = {"need_print": True, "copies": 1, "color": "mono", "sides": "single"}
    intent = validate_intent(data)
    assert intent.copies == 1
    assert intent.color == "mono"


def test_validate_intent_fail_out_of_range():
    """测试：模型发疯输出了 999份，能不能被拦住并抛出异常"""
    data = {"need_print": True, "copies": 999, "color": "mono", "sides": "single"}
    with pytest.raises(ValueError, match="copies 超出范围"):
        validate_intent(data)


def test_validate_intent_fail_wrong_type():
    """测试：模型输出了 copies='三份'，类型错误，能不能被拦住"""
    data = {"need_print": True, "copies": "三份", "color": "mono", "sides": "single"}
    with pytest.raises(ValueError, match="非法 copies"):
        validate_intent(data)