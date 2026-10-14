/*
 * Frida hooks for Vividmoo Pet 2.0.0 (com.vividmoo.vividmoopet, build 516).
 * Class/method names are this build's obfuscated names, verified in smali.
 *
 *   frida -U -f com.vividmoo.vividmoopet -l vividmoo_hooks.js
 *
 * Then log in and use the app (open the box page, start/pause a clean).
 * In the REPL:  summary()   -> per-endpoint signed/authorized table
 *               crcPairs()  -> collected CRC-8 inputs/outputs (JSON)
 *
 * What each section answers:
 *   [keys]    APP_ID / APP_KEY / client device id   (sign is plain Java MD5)
 *   [sign]    md5 input and resulting X-UBT-Sign    (verifies the formula)
 *   [http]    which requests carry X-UBT-Sign / authorization, and its format
 *   [login]   accountType value and password encoding (not the password)
 *   [mqtt]    IM/MQTT credentials and topics
 *   [risp]    outgoing/incoming RISP frames, incl. op_mode/op_state values
 *   [crc]     CRC-8 input/output pairs (libcrclib is native, in a split APK)
 */
'use strict';

const SHOW_SECRETS = true;   // APP_KEY / tokens are needed for the HA config flow
const crc = [];
const endpoints = {};        // "METHOD /path" -> {count, signed, auth}
let appKey = null;

function hex(bytes) {
  if (bytes === null) return '';
  const out = [];
  for (let i = 0; i < bytes.length; i++) out.push(('0' + (bytes[i] & 0xff).toString(16)).slice(-2));
  return out.join('');
}

function mask(value) {
  if (value === null || value === undefined) return String(value);
  value = String(value);
  return SHOW_SECRETS ? value : value.slice(0, 4) + '…(' + value.length + ')';
}

function tryHook(label, fn) {
  try { fn(); console.log('[hook] ' + label); }
  catch (e) { console.log('[hook] FAILED ' + label + ': ' + e); }
}

Java.perform(function () {
  const JString = Java.use('java.lang.String');

  // ---- [keys] ------------------------------------------------------------
  tryHook('UrcsSignInterceptor ctor (a0)', function () {
    const A0 = Java.use('com.ubtrobot.infrastructure.a0');
    A0.$init.overload('java.lang.String', 'java.lang.String', 'java.lang.String').implementation = function (id, key, dev) {
      appKey = key;
      console.log('[keys] APP_ID=' + id + ' APP_KEY=' + mask(key) + ' X-UBT-DeviceId=' + dev);
      return this.$init(id, key, dev);
    };
  });

  tryHook('ProductConfig ctor', function () {
    const PC = Java.use('com.ubtrobot.infrastructure.ProductConfig');
    PC.$init.overload('java.lang.String', 'java.lang.String', 'java.lang.String', 'java.lang.String').implementation = function (id, key, dev, url) {
      console.log('[keys] ProductConfig appId=' + id + ' appKey=' + mask(key) + ' deviceId=' + dev + ' baseUrl=' + url);
      return this.$init(id, key, dev, url);
    };
  });

  // Static holder (jadx: b1.f5568a/f5570c/f5571d). Methods share the names
  // a/c/d, so Frida exposes the fields as _a/_c/_d.
  function dumpStatics() {
    try {
      const B1 = Java.use('androidx.compose.ui.layout.b1');
      console.log('[keys] b1 statics: CHOSEN_BASE_URL=' + B1._a.value + ' APP_ID=' + B1._c.value + ' APP_KEY=' + mask(B1._d.value));
    } catch (e) { console.log('[keys] b1 read failed: ' + e); }
  }
  setTimeout(function () { Java.perform(dumpStatics); }, 4000);
  globalThis.statics = function () { Java.perform(dumpStatics); };

  // ---- [sign] ------------------------------------------------------------
  tryHook('md5 helper h0.f', function () {
    const H0 = Java.use('androidx.compose.animation.core.h0');
    H0.f.overload('java.lang.String').implementation = function (s) {
      const r = this.f(s);
      if (appKey === null || s.indexOf(appKey) !== -1) {
        console.log('[sign] md5("' + (SHOW_SECRETS ? s : s.replace(appKey, '<APP_KEY>')) + '") = ' + r);
      }
      return r;
    };
  });

  // ---- [http] ------------------------------------------------------------
  // nh.f.a(ih.w) is RealInterceptorChain.proceed(Request); it runs once per
  // interceptor, so flags are merged per endpoint and printed when they change.
  tryHook('RealInterceptorChain.proceed (nh.f.a)', function () {
    const Chain = Java.use('nh.f');
    Chain.a.overload('ih.w').implementation = function (req) {
      const text = req.toString();
      const m = /method=([A-Z]+), url=([^,]+)/.exec(text);
      if (m) {
        let path = m[2];
        try { path = new URL(m[2]).pathname; } catch (e) { path = m[2].replace(/^https?:\/\/[^/]+/, '').split('?')[0]; }
        const key = m[1] + ' ' + path;
        const ep = endpoints[key] || (endpoints[key] = { count: 0, signed: false, auth: false });
        const signed = /X-UBT-Sign/i.test(text);
        const auth = /authorization:/i.test(text);
        if (signed && !ep.signed || auth && !ep.auth || ep.count === 0) {
          ep.signed = ep.signed || signed;
          ep.auth = ep.auth || auth;
          console.log('[http] ' + key + ' signed=' + ep.signed + ' auth=' + ep.auth);
          const a = /authorization:([^,\]]*)/i.exec(text);
          if (a) console.log('[http]   authorization value format: ' + mask(a[1].trim()));
        }
        ep.count++;
      }
      const resp = this.a(req);
      return resp;
    };
  });

  globalThis.summary = function () {
    const keys = Object.keys(endpoints).sort(function (x, y) { return x.split(' ')[1] < y.split(' ')[1] ? -1 : 1; });
    console.log('signed auth  count  endpoint');
    keys.forEach(function (k) {
      const e = endpoints[k];
      console.log((e.signed ? '  yes ' : '   NO ') + (e.auth ? '  yes ' : '   no ') + ('     ' + e.count).slice(-6) + '  ' + k);
    });
  };

  // ---- [login] -----------------------------------------------------------
  tryHook('LoginRequestParameter ctor', function () {
    const LP = Java.use('com.ubtrobot.account.airpet.LoginRequestParameter');
    LP.$init.overload('java.lang.String', 'int', 'java.lang.String').implementation = function (account, type, pwd) {
      const p = String(pwd);
      console.log('[login] accountType=' + type + ' password: length=' + p.length +
        ' looksLikeMd5Hex=' + /^[0-9a-f]{32}$/i.test(p) + ' looksLikeSha256Hex=' + /^[0-9a-f]{64}$/i.test(p));
      return this.$init(account, type, pwd);
    };
  });

  function dumpAccountTypes() {
    try {
      const AT = Java.use('com.ubtrobot.account.AccountType');
      const fields = AT.class.getDeclaredFields();
      AT.values().forEach(function (v) {
        const ints = [];
        fields.forEach(function (f) {
          if (f.getType().getName() === 'int') { f.setAccessible(true); ints.push(f.getName() + '=' + f.getInt(v)); }
        });
        console.log('[login] AccountType.' + v.name() + ' ordinal=' + v.ordinal() + ' ' + ints.join(' '));
      });
    } catch (e) { console.log('[login] AccountType dump failed: ' + e); }
  }
  setTimeout(function () { Java.perform(dumpAccountTypes); }, 4000);

  // ---- [mqtt] ------------------------------------------------------------
  tryHook('MqttInfo ctor', function () {
    const MI = Java.use('com.ubtrobot.urcs.connection.MqttInfo');
    MI.$init.overload('com.ubtrobot.urcs.connection.MqttInfo$b').implementation = function (b) {
      this.$init(b);
      console.log('[mqtt] clientId=' + this.getClientId() + ' user=' + this.getUserName() +
        ' password=' + mask(this.getPassword()) + ' pub=' + this.getPubTopic() + ' sub=' + this.getSubTopic() + ' uid=' + this.getUid());
    };
  });

  // ---- [risp] ------------------------------------------------------------
  tryHook('RispMessageConverter (xb.f)', function () {
    const Conv = Java.use('xb.f');
    Conv.a.overload('java.lang.Object').implementation = function (frame) {
      const f = Java.cast(frame, Java.use('cf.a'));
      console.log('[risp] OUT ' + String(f.b.value).replace(/\s+/g, ' ') + ' payload=' + hex(f.e.value));
      return this.a(frame);
    };
    Conv.b.overload('re.k').implementation = function (k) {
      const f = this.b(k);
      if (f !== null) console.log('[risp] IN  ' + String(f.b.value).replace(/\s+/g, ' ') + ' payload=' + hex(f.e.value));
      return f;
    };
  });

  tryHook('DeviceOperateRequest ctor', function () {
    const DOR = Java.use('com.ubtrobot.risp.message.DeviceOperateRequest');
    DOR.$init.overload('com.ubtrobot.risp.message.DeviceOperateRequest$OpMode',
                       'com.ubtrobot.risp.message.DeviceOperateRequest$OpState').implementation = function (mode, state) {
      this.$init(mode, state);
      let payload = '';
      try { payload = hex(this.e()); } catch (e) { payload = 'e() failed: ' + e; }
      console.log('[risp] DeviceOperateRequest ' + mode.toString() + '(' + mode.ordinal() + ') / ' +
        state.toString() + '(' + state.ordinal() + ') -> d2c1019 payload=' + payload);
    };
  });

  // ---- [crc] -------------------------------------------------------------
  tryHook('CrcUtil.a', function () {
    const Crc = Java.use('com.ubtrobot.transport.crcutil.CrcUtil');
    Crc.a.overload('int', '[B').implementation = function (len, data) {
      const r = this.a(len, data);
      if (crc.length < 500) crc.push({ data: hex(data), len: len, crc: r & 0xff });
      return r;
    };
  });
  globalThis.crcPairs = function () { console.log(JSON.stringify(crc)); };
});
