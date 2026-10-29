# JARVIS voice door control

JARVIS (VM100, 10.48.200.211, PHP) is firewall-allowed to reach the door-control shim
(10.48.200.67:8082). It already uses a `kb_intents` DB table for intents + a dispatch switch in
`api/endpoints/chat.php`, with admin gating (`$jIsAdmin`). To add "open the door" voice control:

### 1. Config constants (JARVIS config.php on VM100 — not the repo, holds secrets)
```php
define('PORTCULLIS_URL',   'http://10.48.200.67:8082');
define('PORTCULLIS_TOKEN', '<shim token: sudo cat /etc/portcullis/doorctl.token on the VM>');
```

### 2. Intent row (JARVIS DB `kb_intents`)
```sql
INSERT INTO kb_intents (intent_name, pattern, action_type, response_template, priority, active)
VALUES ('open_door', '(?i)\\b(open|unlock)\\b.*\\bdoor\\b', 'action', '', 90, 1);
```

### 3. Handler (add a case in chat.php dispatch switch, mirrors restart_agent)
```php
case 'open_door':
    if (!$jIsAdmin) { $reply = "Opening doors needs an administrator, {$userAddr}."; $source='intent:open_door'; break; }
    $msgLow = strtolower($message);
    $door = null;
    if (preg_match('/door\s*([1-4])/', $msgLow, $m)) $door = (int)$m[1];
    // TODO: map physical names once known, e.g. front->1, shed->2 ...
    if (!$door) { $reply = "Which door, {$userAddr}? e.g. 'open door 1'."; $source='intent:open_door'; break; }
    if (!defined('PORTCULLIS_URL')) { $reply = "Door controller isn't configured, {$userAddr}."; $source='intent:open_door'; break; }
    $ctx = stream_context_create(['http'=>['method'=>'POST','timeout'=>8,
        'header'=>"Authorization: Bearer ".PORTCULLIS_TOKEN."\r\nContent-Type: application/json",
        'content'=>'{}','ignore_errors'=>true]]);
    $res = @json_decode(@file_get_contents(PORTCULLIS_URL."/open/".$door, false, $ctx), true);
    $reply = ($res['ok'] ?? false)
        ? "Door {$door} unlocked, {$userAddr}."
        : "I couldn't reach the door controller, {$userAddr}.";
    $source = 'intent:open_door';
    break;
```

### 4. Deploy
Commit + push the `jarvis` repo (auto-deploys to VM100). Add the config constants + run the SQL on VM100.

**Note:** door-open by voice is a physical, admin-only action. Confirm the door-number → physical-name
mapping before relying on it, and treat the first real actuation as the after-hours door-open test.
