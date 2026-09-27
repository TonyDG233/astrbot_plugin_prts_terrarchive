"""跨层错误类型。

ContractError 对应上游契约层错误（code/retryable 会进入模型可见的错误响应）；
InstallerFault 对应 installer.js 的 InstallerFault（code 用于命令/UI 提示）。
"""


class ContractError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class InstallerFault(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message