import pytest

from rohanboard.exec import LocalExecutor


@pytest.mark.asyncio
async def test_local_executor_echo():
    ex = LocalExecutor()
    out = await ex.run(["/bin/echo", "hello"])
    assert out.strip() == "hello"


@pytest.mark.asyncio
async def test_local_executor_failure():
    ex = LocalExecutor()
    with pytest.raises(RuntimeError, match=r"rc=\d+"):
        await ex.run(["/bin/false"])
