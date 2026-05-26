## Plan: Telegram Album 一致性支援（可執行版）

目標：
- Live 與 Past 模式都支援 Telegram album。
- 兩個模式共用同一條 forwarding pipeline。
- 使用 grouped_id 識別 album，無 grouped_id 當 single message。
- Live 模式使用 1000ms debounce 聚合相簿。
- cannot forward 時自動 fallback 到 download-then-upload。
- 429/FloodWait 時等待後整組重試。

## 假設與非目標

假設：
- source 訊息可取得 grouped_id（有相簿時）。
- Telethon 在現有版本可提供 live 事件與歷史訊息 grouped_id。
- 現有插件處理單則訊息有效。

非目標：
- 不改動插件設計本身，只做轉發管線整合。
- 不新增與需求無關的 UI 功能。
- 不做大規模重構，只做必要改動。

## 成功標準（可驗證）

1. Live：同一 grouped_id 的相簿在 1000ms 內聚合為一組處理。
2. Past：歷史訊息按 grouped_id 分組且順序一致。
3. 兩模式都調用同一 forwarding 入口，不存在雙實作。
4. cannot forward 會自動 download-then-upload 成功送出（在有權限前提下）。
5. 429/FloodWait 發生時，等待後整組重試，不留下半組殘留。
6. 權限錯誤快速失敗並記錄，不盲目重試。

## 實作步驟（每步附 verify）

1. 定義共用資料模型與映射鍵
- 實作內容：
	- 建立 ForwardUnit（single/album 皆可承載）。
	- 鍵設計：album_key=(source_chat_id, grouped_id)，single_key=(source_chat_id, message_id)。
	- 映射同時保存相簿級與單項級，供 reply/edit/delete 查找。
- verify：
	- 單元測試可正確生成 album_key 與 single_key。
	- 相同來源重覆事件不會新增重覆映射。

2. 抽出共用 forwarding pipeline
- 實作內容：
	- 建立唯一入口（例如 forward_unit）。
	- 流程：嘗試 forward -> 失敗判斷 -> fallback reupload（必要時）-> 寫映射。
	- 錯誤分類：429/FloodWait、權限、不可轉發、暫時性其他錯誤、永久錯誤。
- verify：
	- Live/Past 的調用點都進入同一入口。
	- 測試可觀察到錯誤分類結果一致。

3. 加入 cannot-forward fallback
- 實作內容：
	- 遇到 cannot forward 類錯誤，自動 download-then-upload。
	- album fallback 保持原順序。
	- caption anchor 固定第一項。
	- 臨時檔在成功或失敗後清理。
- verify：
	- 模擬不可轉發來源時仍可到達目標 chat。
	- 工作目錄無殘留臨時檔。

4. 實作原子相簿與重試策略
- 實作內容：
	- 相簿以目標 chat 為原子單位。
	- 任一 item 失敗，先回滾該目標已送出項目，再整組重試。
	- 429/FloodWait：按 Telegram 回傳秒數等待後整組重試。
	- 非 429 可恢復錯誤：1s -> 2s -> 4s，最多 3 次。
	- 權限/無效目標：快速失敗。
- verify：
	- 注入 429 後可在等待後成功重送。
	- 中途失敗不會留下半組訊息。

5. Live 模式接線（1000ms debounce）
- 實作內容：
	- grouped_id 訊息進 buffer。
	- 1000ms 無新訊息時 flush。
	- flush 前按 message_id 排序後送共用 pipeline。
	- 無 grouped_id 直接 single 流程。
- verify：
	- 連續送 3 張圖只觸發一次 album flush。
	- 單則訊息不受 debounce 影響。

6. Past 模式接線（同一 pipeline）
- 實作內容：
	- iter_messages 時按 grouped_id 聚合。
	- 聚合結果按 message_id 排序再送共用 pipeline。
	- 無 grouped_id 按 single 處理。
- verify：
	- 歷史回放相簿分組與原始訊息一致。
	- single 與 album 都走同一 forwarding 入口。

7. reply/edit/delete 映射語義
- 實作內容：
	- reply 到 album 映射到目標端第一個成功 item。
	- delete album 時刪除目標端整組映射。
	- edit 採 Telegram 能力範圍內處理，不能直接 edit 的 media 明確記錄限制。
- verify：
	- reply 對位正確。
	- delete 不殘留孤兒訊息。

8. 配置項落地
- 實作內容：
	- album_debounce_ms=1000
	- album_atomic=true
	- forward_fallback_to_reupload=true
	- retry_on_429=true
	- retry_backoff_base_ms=1000
	- retry_max_attempts_for_non_429=3
- verify：
	- 設定值可讀可生效。
	- 預設值符合需求。

## 檔案影響範圍

- tgcf/live.py
- tgcf/past.py
- tgcf/utils.py
- tgcf/storage.py
- tgcf/config.py

## 風險與對應

1. 風險：429 長時間重試導致佇列積壓。
- 對應：記錄每組重試次數與最後錯誤，必要時提供人工干預。

2. 風險：權限錯誤被誤判為暫時性。
- 對應：明確錯誤分類，權限類直接 fail-fast。

3. 風險：album 回滾期間出現刪除失敗。
- 對應：刪除失敗也要落 log，並阻止該組被標記為成功。

## 已確認決策

- Live debounce 固定 1000ms。
- album 要求原子一致（每個目標 chat）。
- cannot forward 必須 fallback 到 download-then-upload。