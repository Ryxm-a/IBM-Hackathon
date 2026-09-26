def add(a, b):
    return a + b


if __name__ == "__main__":
    result = add(3, 4)
    print(f"3 + 4 = {result}")
    assert result == 7, "add() returned wrong value"
    print("OK")
