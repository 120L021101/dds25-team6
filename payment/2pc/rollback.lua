-- rollback.lua
local user_key = KEYS[1]

local lock_key = user_key .. ":lock"
local txn_key = user_key .. ":txn"

local txn_id = ARGV[1] 

-- 检查事务状态
if redis.call("GET", txn_key) == "PREPARED:" .. txn_id then
    redis.call("DEL", txn_key)  -- 删除事务日志
    redis.call("DEL", lock_key) -- 释放锁
    return "ROLLED BACK"
else
    return "NO TRANSACTION"
end
