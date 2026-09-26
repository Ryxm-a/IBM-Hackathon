from check import check


def test_check_returns_true_for_1():
    assert check(1) is True


def test_check_returns_false_for_1():
    assert check(1) is False
