import config
print("DRY_RUN:", config.DRY_RUN)
assert config.DRY_RUN is True, "DRY_RUN is not True"
