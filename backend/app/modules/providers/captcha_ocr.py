"""明略验证码识别子进程：图片从 stdin 读取，只输出识别文本。"""

import contextlib
import io
import sys


def main() -> None:
    image = sys.stdin.buffer.read(1024 * 1024 + 1)
    if not image or len(image) > 1024 * 1024:
        raise SystemExit(1)
    # 第三方库可能输出初始化信息，不能混入识别结果或业务日志。
    with contextlib.redirect_stdout(io.StringIO()):
        import ddddocr

        code = ddddocr.DdddOcr(show_ad=False).classification(image)
    if not isinstance(code, str):
        raise SystemExit(1)
    sys.stdout.write("".join(ch for ch in code if ch.isascii() and ch.isalnum()))


if __name__ == "__main__":
    main()
