import sys
import time
import hmac
import hashlib
import json
import os
import base64
import uuid
import random
import sqlite3
from datetime import datetime
from colorama import init
import pyotp

import requests
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLineEdit, QTextEdit, QLabel, QGroupBox,
    QCheckBox, QSpinBox, QMessageBox, QProgressBar, QScrollArea,
    QGridLayout, QDialog, QDialogButtonBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont

init(autoreset=True)

# ========================== CƠ SỞ DỮ LIỆU ==========================
class Database:
    def __init__(self, db_file="data.db"):
        self.db_file = db_file
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password_enc TEXT,
            secret_enc TEXT,
            token TEXT,
            uid TEXT,
            name TEXT,
            jobs_done INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        conn.commit()
        conn.close()

    def add_account(self, email, password_enc, secret_enc, token, uid, name):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO accounts 
            (email, password_enc, secret_enc, token, uid, name, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
            (email, password_enc, secret_enc, token, uid, name))
        conn.commit()
        conn.close()

    def update_account_token(self, uid, token):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('''UPDATE accounts SET token = ?, updated_at = CURRENT_TIMESTAMP WHERE uid = ?''', (token, uid))
        conn.commit()
        conn.close()

    def update_account_jobs(self, uid, jobs_done):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('''UPDATE accounts SET jobs_done = jobs_done + ?, updated_at = CURRENT_TIMESTAMP WHERE uid = ?''', (jobs_done, uid))
        conn.commit()
        conn.close()

    def delete_account(self, uid):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('DELETE FROM accounts WHERE uid = ?', (uid,))
        conn.commit()
        conn.close()

    def get_all_accounts(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('SELECT email, password_enc, secret_enc, token, uid, name, jobs_done FROM accounts')
        rows = c.fetchall()
        conn.close()
        accounts = []
        for row in rows:
            accounts.append({
                'email': row[0],
                'password_enc': row[1],
                'secret_enc': row[2],
                'token': row[3],
                'uid': row[4],
                'name': row[5],
                'jobs_done': row[6]
            })
        return accounts

    def get_setting(self, key, default=None):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key = ?', (key,))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0]
        return default

    def set_setting(self, key, value):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        conn.commit()
        conn.close()

    def get_all_settings(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings')
        rows = c.fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}

# ========================== MÃ HÓA ==========================
def encrypt_text(text):
    if not text:
        return ""
    return base64.b64encode(text.encode()).decode()

def decrypt_text(encrypted):
    if not encrypted:
        return ""
    return base64.b64decode(encrypted.encode()).decode()

# ========================== PHẦN API FACEBOOK ==========================
class Requests_facebook:
    def __init__(self):
        self._requests = requests.Session()
        self._requests.headers.update({
            "User-Agent": "[FBAN/FB4A;FBAV/571.0.0.44.73;FBBV/1023572511;FBDM/{density=1.5,width=540,height=960};FBLC/en_US;FBRV/0;FBCR/Verizon;FBMF/samsung;FBBD/samsung;FBPN/com.facebook.katana;FBDV/SM-S901N;FBSV/9;FBOP/1;FBCA/x86_64:arm64-v8a;]",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Priority": "u=0",
            "app-scope-id-header": "77a256c1-5911-48a4-bafd-fa4316c9f941",
            "x-fb-client-ip": "True",
            "x-fb-conn-uuid-client": "AS4k/LynmUQ6y62aIVsNWw==",
            "X-FB-Connection-Type": "WIFI",
            "x-fb-device-group": "1904",
            "X-FB-HTTP-Engine": "Tigon/Liger",
            "X-FB-Net-HNI": "310005",
            "X-FB-Request-Analytics-Tags": '{"network_tags":{"product":"350685531728","request_category":"graphql","purpose":"fetch","retry_attempt":"0"},"application_tags":"graphservice"}',
            "x-fb-rmd": "fail=Server:INVALID_MAP,Default:INVALID_MAP;v=;ip=;tkn=;reqTime=1633641062;recvTime=0",
            "x-fb-server-cluster": "True",
            "X-FB-SIM-HNI": "310005",
            "x-graphql-client-library": "graphservice",
            "x-graphql-request-purpose": "fetch",
            "X-Meta-USDID": "28266e60-2c0c-48f2-b64d-d25fee3d714e.1785300592.MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAELj5WOTDR3Uvqc5tqJ5lWdL_Mdi5xxeE342NEI82WLzzDIBKX4Q6wQ7hwtZDInM4tf_pkkNKKsoBQo_2cQQbIvQ.MEUCIQCx1YdU9dWWQRQ-bxEA83dBGnCWy5pAm3ed5KQUnoQlSAIgX_ISrCeup82fNh7OBMl-jZQ18eNbmooTjFzt6Ne12XY",
            "X-Meta-ZCA": "empty_token",
            "x-tigon-is-retry": "False",
            "x-zero-eh": "664c0faaac849cb891d0a261fbb72a12",
            "X-ZERO-STATE": "unknown",
        })
        self._url_api = 'https://b-graph.facebook.com/graphql'

    def _login_facebook(self, uid, password, encode_2fa):
        ts = int(time.time())
        device = str(uuid.uuid4())
        client_trace_id = str(uuid.uuid4())
        waterfall_id = str(uuid.uuid4())

        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "CreateOneTimeChallengeNonceCreate",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '14372771303129768114139595837',
            'fb_api_client_context': '{"is_background":false}',
            'variables': r'''{"input":{"app_scoped_device_id":"%s"}}''' % (device),
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }

        res = self._requests.post(url=self._url_api, headers={
            "Authorization": "OAuth 350685531728|62f8ce9f74b12f84c123cc23437a4a32",
            "X-FB-Friendly-Name": "CreateOneTimeChallengeNonceCreate",
            "X-ZERO-F-DEVICE-ID": device
        }, data=payload)

        data = res.json()

        data = {
            "challenge_nonce": data["data"]["xfb_fb_key_attestation_create_one_time_challenge_nonce"]["challenge_nonce"],
            "username": uid
        }
        data_challenge_nonce = base64.b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode()
        client_trace_id = str(uuid.uuid4())

        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "FbBloksActionRootQuery-com.bloks.www.bloks.caa.login.async.send_login_request",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '119940804212792425089909966247',
            'fb_api_client_context': '{"is_background":false}',
            'variables': r'''{"params":{"params":"{\"params\":\"{\\\"server_params\\\":{\\\"device_id\\\":\\\"%s\\\",\\\"server_login_source\\\":\\\"login\\\",\\\"waterfall_id\\\":\\\"%s\\\",\\\"attestation_result\\\":{\\\"keyHash\\\":\\\"c816e3d25740b8692da6c3e168baf6d38c3e3b855b10f114a6900aa70e386bf2\\\",\\\"signature\\\":\\\"MEUCIQDhOJq8dMQu5S1SWRENyPxV6kMSmaXCAXGuLQnF\\/6wdCAIgBr\\/suDJIvrhXd\\/iWyNlCQUv1pUd8WoEW5pawWtqlpIA=\\\",\\\"data\\\":\\\"%s\\\"},\\\"machine_id\\\":\\\"p4Rpar0qseBOPtkrB_exw55d\\\",\\\"from_native_screen\\\":true,\\\"credential_type\\\":\\\"password\\\",\\\"password\\\":\\\"#PWD_FB4A:0:%s:%s\\\",\\\"try_num\\\":\\\"1\\\",\\\"family_device_id\\\":\\\"%s\\\",\\\"event_flow\\\":\\\"login_manual\\\",\\\"event_step\\\":\\\"home_page\\\",\\\"is_from_logged_in_switcher\\\":false,\\\"contact_point\\\":\\\"%s\\\"}}\"}","bloks_versioning_id":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f","app_id":"com.bloks.www.bloks.caa.login.async.send_login_request"},"scale":"1.5","nt_context":{"using_white_navbar":true,"styles_id":"a07b73c926a84224348806e6cd486365","pixel_ratio":1.5,"is_push_on":true,"debug_tooling_metadata_token":null,"is_flipper_enabled":false,"theme_params":[{"value":["three_neutral_gray"],"design_system_name":"XMDS"},{"value":[],"design_system_name":"FDS"}],"bloks_version":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"}}''' % (device, waterfall_id, data_challenge_nonce, ts, password, device, str(uid)),
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }

        res = self._requests.post(url=self._url_api, headers={
            "Authorization": "OAuth 350685531728|62f8ce9f74b12f84c123cc23437a4a32",
            "X-FB-Friendly-Name": "FbBloksActionRootQuery-com.bloks.www.bloks.caa.login.async.send_login_request"
        }, data=payload)

        data = res.text
        try:
            two_step_verification_context = data.split(r'''(dkc \\\"two_step_verification_context\\\" \\\"flow_source\\\" \\\"machine_id\\\" \\\"device_id\\\" \\\"should_fallback_to_sms\\\" \\\"family_device_id\\\" \\\"INTERNAL_INFRA_screen_id\\\") (dkc \\\"''')[1].split(r'''\\\"''')[0]

            secret = encode_2fa.replace(" ", "")
            totp = pyotp.TOTP(secret)

            payload = {
                "method": "post",
                "format": "json",
                "server_timestamps": "true",
                "locale": "en_US",
                "purpose": "fetch",
                "fb_api_req_friendly_name": "FbBloksActionRootQuery-com.bloks.www.two_step_verification.verify_code.async",
                'fb_api_caller_class': 'graphservice',
                'client_doc_id': '119940804212792425089909966247',
                'fb_api_client_context': '{"is_background":false}',
                'variables': r'''{"params":{"params":"{\"params\":\"{\\\"client_input_params\\\":{\\\"auth_secure_device_id\\\":\\\"\\\",\\\"block_store_machine_id\\\":null,\\\"code\\\":\\\"%s\\\",\\\"should_trust_device\\\":1,\\\"family_device_id\\\":\\\"77a256c1-5911-48a4-bafd-fa4316c9f941\\\",\\\"device_id\\\":\\\"77a256c1-5911-48a4-bafd-fa4316c9f941\\\",\\\"cloud_trust_token\\\":null,\\\"network_bssid\\\":null,\\\"machine_id\\\":\\\"\\\"},\\\"server_params\\\":{\\\"INTERNAL__latency_qpl_marker_id\\\":36707139,\\\"device_id\\\":null,\\\"spectra_reg_login_data\\\":null,\\\"challenge\\\":\\\"totp\\\",\\\"INTERNAL__latency_qpl_instance_id\\\":130518416400274,\\\"two_step_verification_context\\\":\\\"%s\\\",\\\"flow_source\\\":\\\"two_factor_login\\\"}}\"}","bloks_versioning_id":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f","app_id":"com.bloks.www.two_step_verification.verify_code.async"},"scale":"1.5","nt_context":{"using_white_navbar":true,"styles_id":"a07b73c926a84224348806e6cd486365","pixel_ratio":1.5,"is_push_on":true,"debug_tooling_metadata_token":null,"is_flipper_enabled":false,"theme_params":[{"value":["three_neutral_gray"],"design_system_name":"XMDS"},{"value":[],"design_system_name":"FDS"}],"bloks_version":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"}}''' % (totp.now(), two_step_verification_context),
                'fb_api_analytics_tags': '["GraphServices"]',
                'client_trace_id': '73b659d1-a3cc-4db4-bc79-892b41b88f2c'
            }
            res = self._requests.post(url=self._url_api, headers={
                "Authorization": "OAuth 350685531728|62f8ce9f74b12f84c123cc23437a4a32",
                "X-FB-Friendly-Name": "FbBloksActionRootQuery-com.bloks.www.two_step_verification.verify_code.async"
            }, data=payload)
            data = res.text
        except:
            pass

        accesstoken = data.split(r'''"access_token\\\\\\\":\\\\\\\"''')[1].split(r'''\\\\\\\"''')[0]
        return accesstoken

    def login_and_get_token(self, email, password, secret_2fa=""):
        try:
            token = self._login_facebook(email, password, secret_2fa)
            return token
        except Exception as e:
            print(f"Lỗi đăng nhập: {e}")
            return None

def generate_session_id(prefix="UFS", suffix="fg-4"):
    unique_id = uuid.uuid4()
    return f"{prefix}-{unique_id}-{suffix}"

def generate_fragment(name: str, source: str, duration: float = 0.0):
    now = time.time()
    return {
        'name': name,
        'source': source,
        'start_time': now,
        'end_time': now + duration,
        'instance_id': random.randint(100000, 999999999)
    }

def build_attribution(fragments):
    items = []
    for f in fragments:
        item = f"{f['name']},{f['source']},,{f['start_time']},{f['instance_id']},,,,{f['end_time']}"
        items.append(item)
    return ';'.join(items)

class AUTO_Action:
    def __init__(self):
        self.url = 'https://b-graph.facebook.com/graphql'
        self._requests = requests.Session()
        self.id_reaction = {
            'LIKE': 1635855486666999,
            'LOVE': 1678524932434102,
            'CARE': 613557422527858,
            'HAHA': 115940658764963,
            'WOW': 478547315650144,
            'SAD': 908563459236466,
            'ANGRY': 444813342392137,
        }
        self._requests.headers.update({
            "User-Agent": "[FBAN/FB4A;FBAV/571.0.0.44.73;FBBV/1023572511;FBDM/{density=1.5,width=540,height=960};FBLC/en_US;FBRV/0;FBCR/Verizon;FBMF/samsung;FBBD/samsung;FBPN/com.facebook.katana;FBDV/SM-S901N;FBSV/9;FBOP/1;FBCA/x86_64:arm64-v8a;]",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Priority": "u=0",
            "app-scope-id-header": "77a256c1-5911-48a4-bafd-fa4316c9f941",
            "x-fb-client-ip": "True",
            "x-fb-conn-uuid-client": "AS4k/LynmUQ6y62aIVsNWw==",
            "X-FB-Connection-Type": "WIFI",
            "x-fb-device-group": "1904",
            "X-FB-HTTP-Engine": "Tigon/Liger",
            "X-FB-Net-HNI": "310005",
            "X-FB-Request-Analytics-Tags": '{"network_tags":{"product":"350685531728","request_category":"graphql","purpose":"fetch","retry_attempt":"0"},"application_tags":"graphservice"}',
            "x-fb-rmd": "fail=Server:INVALID_MAP,Default:INVALID_MAP;v=;ip=;tkn=;reqTime=1633641062;recvTime=0",
            "x-fb-server-cluster": "True",
            "X-FB-SIM-HNI": "310005",
            "x-graphql-client-library": "graphservice",
            "x-graphql-request-purpose": "fetch",
            "X-Meta-USDID": "28266e60-2c0c-48f2-b64d-d25fee3d714e.1785300592.MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAELj5WOTDR3Uvqc5tqJ5lWdL_Mdi5xxeE342NEI82WLzzDIBKX4Q6wQ7hwtZDInM4tf_pkkNKKsoBQo_2cQQbIvQ.MEUCIQCx1YdU9dWWQRQ-bxEA83dBGnCWy5pAm3ed5KQUnoQlSAIgX_ISrCeup82fNh7OBMl-jZQ18eNbmooTjFzt6Ne12XY",
            "X-Meta-ZCA": "empty_token",
            "x-tigon-is-retry": "False",
            "x-zero-eh": "664c0faaac849cb891d0a261fbb72a12",
            "X-ZERO-STATE": "unknown",
            "X-ZERO-F-DEVICE-ID": '77a256c1-5911-48a4-bafd-fa4316c9f941'
        })

    def get_info(self, token):
        client_trace_id = str(uuid.uuid4())
        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "FbStoriesLightBucketsQuery",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '134400997213594131792470699723',
            'fb_api_client_context': '{"is_background":false}',
            'variables': r'''{"video_thumbnail_width":336,"profile_image_size":60,"skip_top_tray_ats_mid_card_offset":true,"story_media_height":960,"skip_inorganic_buckets":true,"story_media_width":540,"use_native_entrypoint_for_stars_on_reels":true,"enable_hd":true,"scale":"1.5","nt_context":{"using_white_navbar":true,"styles_id":"a07b73c926a84224348806e6cd486365","pixel_ratio":1.5,"is_push_on":true,"debug_tooling_metadata_token":null,"is_flipper_enabled":false,"theme_params":[{"value":["three_neutral_gray"],"design_system_name":"XMDS"},{"value":["DARKER_PRIMARY_DEEMPHASIZED_BUTTON_BACKGROUND_TEST"],"design_system_name":"FDS"}],"bloks_version":"64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"},"use_server_thumbnail":true,"enable_content_sharing_in_notes":true,"enable_uses_suggested_lyrics":true,"is_final_ranking_score_enabled":true,"enable_gif_note":true,"fbstory_tray_preview_height":295,"msqrd_supported_capabilities":[{"value":"multiplane_disabled","name":"multiplane"},{"value":"world_tracker_enabled","name":"world_tracker"},{"value":"xray_disabled","name":"xray"},{"value":"world_tracking_disabled","name":"world_tracking"},{"value":"half_float_render_pass_enabled","name":"half_float_render_pass"},{"value":"multiple_render_targets_enabled","name":"multiple_render_targets"},{"value":"vertex_texture_fetch_enabled","name":"vertex_texture_fetch"},{"value":"render_settings_high_enabled","name":"render_settings_high"},{"value":"body_tracking_enabled","name":"body_tracking"},{"value":"gyroscope_enabled","name":"gyroscope"},{"value":"geoanchor_disabled","name":"geoanchor"},{"value":"scene_depth_disabled","name":"scene_depth"},{"value":"182,202","name":"supported_beta_sdk_versions"},{"value":"segmentation_enabled","name":"segmentation"},{"value":"hand_tracking_enabled","name":"hand_tracking"},{"value":"real_scale_estimation_disabled","name":"real_scale_estimation"},{"value":"hair_segmentation_disabled","name":"hair_segmentation"},{"value":"depth_shader_read_enabled","name":"depth_shader_read"},{"value":"etc2_compression","name":"compression"},{"value":"0","name":"face_tracker_version"},{"value":"149.0,150.0,151.0,152.0,153.0,154.0,155.0,156.0,157.0,158.0,159.0,160.0,161.0,162.0,163.0,164.0,165.0,166.0,167.0,168.0,169.0,170.0,171.0,172.0,173.0,174.0,175.0,176.0,177.0,178.0,179.0,180.0,181.0,182.0,183.0,184.0,185.0,186.0,187.0,188.0,189.0,190.0,191.0,192.0,193.0,194.0,195.0,196.0,197.0,198.0,199.0,200.0,201.0,202.0","name":"supported_sdk_versions"}],"min_story_page_size_from_client":4,"enable_status_consumption":true,"should_include_first_media":true,"video_thumbnail_height":336,"fetch_bucket_content_hash":true,"should_fetch_midcard_ranking_tracking_string":true,"fbstory_tray_sizing_type":"cover-fill-cropped","should_fetch_model_for_thumbnail_frame":true,"fbstory_tray_preview_width":166,"enable_cix_screen_rollout":true,"client_request_id":"4175cf24-7ea8-4a9a-8b1e-91f9f0da9077","is_hybrid_tray_enabled":true,"should_fetch_is_trending_bucket_field":true,"page_profile_image_size_experiment":60,"should_include_production_pog_data":true,"should_include_expiration_time":true,"should_include_feedback_summary":true,"should_include_live_media":true,"should_include_live_tile_metadata":true,"show_production_pog_image":true,"unified_stories_buckets_paginated_for_light_query_first":6,"should_fetch_profile_navigation_info":true,"should_skip_feedback_total_count":true,"should_include_secondary_text_for_non_self_pog":true,"enable_easter_egg_v2":true,"include_inform_treatment":true,"compose_surface":"STORIES"}''',
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }
        res = self._requests.post(url=self.url, headers={
            "Authorization": f"OAuth {token}",
            "X-FB-Friendly-Name": "FbStoriesLightBucketsQuery",
        }, data=payload)
        try:
            res_json = res.json()
        except:
            return None, "Lỗi phân tích JSON (có thể token hỏng)"
        if "errors" in res_json:
            for err in res_json["errors"]:
                if "access_token" in err.get("message", "").lower():
                    return None, "Token đã hết hạn hoặc không hợp lệ"
                return None, err.get("message", "Lỗi GraphQL")
        user_id = res_json.get("data", {}).get("me", {}).get("id")
        if user_id:
            return user_id, "OK"
        return None, "Không tìm thấy user_id trong phản hồi (có thể token hỏng)"

    def auto_reaction(self, post_id, reaction_type, token, actor_id):
        raw = f"feedback:{post_id}"
        feedback_id = base64.b64encode(raw.encode()).decode()
        fragments = [
            generate_fragment("NewsFeedFragment", "native_newsfeed", duration=55.0),
            generate_fragment("PermalinkDialogFragment", "story_view", duration=0.0)
        ]
        attribution_str = build_attribution(fragments)
        session_id = generate_session_id()
        ts = int(time.time())
        client_trace_id = str(uuid.uuid4())
        variables = {
            "profile_image_size": 60,
            "input": {
                "tracking": None,
                "session_id": session_id,
                "feedback_source": "native_newsfeed",
                "feedback_referrer": "native_newsfeed",
                "feedback_id": feedback_id,
                "feedback_reaction_id": self.id_reaction[reaction_type],
                "downstream_share_session_id": None,
                "downstream_share_session_start_time": None,
                "downstream_share_session_origin_uri": None,
                "nectar_module": "newsfeed_ufi",
                "attribution_id_v2": attribution_str,
                "actor_id": actor_id,
                "downstream_share_session_navigation_events_count": [
                    {"name": "APP_BACKGROUNDED", "count": 0},
                    {"name": "APP_FOREGROUNDED", "count": 0}
                ],
                "action_timestamp": ts
            },
            "is_comment_sharing_enabled": True
        }
        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "ViewerReactionsMutation",
            'fb_api_caller_class': 'graphservice',
            'client_doc_id': '285778409311568856348218907304',
            'fb_api_client_context': json.dumps({"is_background": False}),
            'variables': json.dumps(variables),
            'fb_api_analytics_tags': '["GraphServices"]',
            'client_trace_id': client_trace_id
        }
        res = self._requests.post(url=self.url, headers={
            "Authorization": f"OAuth {token}",
            "X-FB-Friendly-Name": "ViewerReactionsMutation",
        }, data=payload)
        try:
            result = res.json()
        except:
            return False
        if "errors" in result:
            return False
        feedback = result.get("data", {}).get("feedback_react", {}).get("feedback", {})
        if feedback.get("viewer_feedback_reaction_key") is not None:
            return True
        else:
            return False

    def auto_cmt(self, post_id, text, token, actor_id):
        raw = f"feedback:{post_id}"
        feedback_id = base64.b64encode(raw.encode()).decode()
        fragments = [
            generate_fragment("NewsFeedFragment", "native_newsfeed", duration=55.0),
            generate_fragment("PermalinkDialogFragment", "story_view", duration=0.0)
        ]
        attribution_str = build_attribution(fragments)
        session_id = generate_session_id()
        ts = int(time.time())
        client_trace_id = str(uuid.uuid4())
        idempotence_token = str(uuid.uuid4())

        variables = {
            "query_for_can_be_invited_by_viewer": True,
            "image_medium_width": 270,
            "image_low_height": 2048,
            "image_medium_height": 2048,
            "profile_image_size": 60,
            "media_type": "image/x-auto",
            "image_high_width": 540,
            "input": {
                "tracking": None,
                "skip_link_usage_quota_validation": False,
                "session_id": session_id,
                "privacy_type": "DEFAULT_PRIVACY",
                "feedback_source": "feedback_comments",
                "vod_video_timestamp": 0,
                "downstream_share_session_id": None,
                "downstream_share_session_start_time": None,
                "downstream_share_session_origin_uri": None,
                "nectar_module": "newsfeed_ufi",
                "actor_id": actor_id,
                "attribution_id_v2": attribution_str,
                "action_timestamp": ts,
                "ad_imp_token": None,
                "message": {
                    "text": text,
                    "ranges": []
                },
                "feedback_referrer": "native_newsfeed",
                "feedback_id": feedback_id,
                "idempotence_token": idempotence_token,
                "entry_point": "TAP_FOOTER_COMMENT",
                "downstream_share_navigation_events_count": [
                    {"name": "APP_BACKGROUNDED", "count": 0},
                    {"name": "APP_FOREGROUNDED", "count": 0}
                ]
            },
            "enable_participation_questionnaire": True,
            "enable_reactions_dock_message": True,
            "image_low_width": 180,
            "enable_user_signals_in_comments": True,
            "enable_comment_shares": True,
            "nt_context": {
                "using_white_navbar": True,
                "styles_id": "a07b73c926a84224348806e6cd486365",
                "pixel_ratio": 1.5,
                "is_push_on": True,
                "debug_tooling_metadata_token": None,
                "is_flipper_enabled": False,
                "theme_params": [
                    {"value": ["three_neutral_gray"], "design_system_name": "XMDS"},
                    {"value": ["DARKER_PRIMARY_DEEMPHASIZED_BUTTON_BACKGROUND_TEST"], "design_system_name": "FDS"}
                ],
                "bloks_version": "64b572e5ffae5e9f072424a656577ac0bb01a187d02b7a00bbfb3a08497c3d6f"
            },
            "fetch_presence_eligible": True,
            "image_high_height": 2048,
            "fetch_privacy_value_for_pending_approval_comment": True,
            "fetch_privacy_value_for_declined_comment": True,
            "should_fetch_pinned_comment": True,
            "enable_story_ring": True,
            "should_fetch_content_owner_reaction": True,
            "group_member_action_source": "GROUP_COMMENT",
            "feedback_source": "NEWS_FEED",
            "enable_add_friend_in_comments": True,
            "should_fetch_comments_downvote_fields": True,
            "size_style": "contain-fit",
            "enable_identity_badge_v2": True,
            "enable_comment_identity_badge": True,
            "profile_pic_media_type": "image/x-auto",
            "enable_comment_pin_v2": True,
            "enable_gen_ai_content_transparency_in_comments": True
        }

        variables_str = json.dumps(variables, ensure_ascii=False)
        payload = {
            "method": "post",
            "format": "json",
            "server_timestamps": "true",
            "locale": "en_US",
            "purpose": "fetch",
            "fb_api_req_friendly_name": "CommentCreateMutation",
            "fb_api_caller_class": "graphservice",
            "client_doc_id": "8474489818219302131872116307",
            "fb_api_client_context": json.dumps({"is_background": False}),
            "variables": variables_str,
            "fb_api_analytics_tags": r'''["nav_attribution_id_v2=%s","GraphServices","surface_hierarchy=FeedbackFragment,story_feedback_flyout,null;SimpleUFIPopoverFragment,story_feedback_flyout,null;NewsFeedFragment,native_newsfeed,null;FbChromeFragment,unknown,null;FbMainTabActivity,native_newsfeed,null","visitation_id=null","session_id=%s"]''' % (attribution_str,session_id),
            "client_trace_id": client_trace_id
        }
        res = self._requests.post(url=self.url, headers={
            "Authorization": f"OAuth {token}",
            "X-FB-Friendly-Name": "CommentCreateMutation",
        }, data=payload)
        try:
            result = res.json()
        except:
            return False
        if "errors" in result:
            return False
        else:
            comment_data = result.get("data", {}).get("comment_create", {}).get("comment")
            if comment_data and comment_data.get("id"):
                return True
            else:
                return False

# ========================== PHẦN TTC ==========================
class requests_ttc:
    def __init__(self):
        self.httpx = requests.Session()

    def get_accout(self, access_token, url="https://tuongtaccheo.com/logintoken.php"):
        data = {"access_token": access_token}
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }
        response = self.httpx.post(url, data=data, headers=headers)
        if response.json()['status'] == "success":
            cookie = response.headers['Set-Cookie']
            main_cookie = cookie.split(';')[0]
            return response.json()['data'], main_cookie
        else:
            return "Sai access_token hoặc tài khoản không tồn tại", ""

    def get_jobs_like_vip(self, cookie, uid, url="https://tuongtaccheo.com/kiemtien/likepostvipcheo/getpost.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self.httpx.get(url, params={'nickchay': uid}, headers=headers)
        return response.json()

    def get_jobs_like_re(self, cookie, uid, url="https://tuongtaccheo.com/kiemtien/likepostvipre/getpost.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self.httpx.get(url, headers=headers)
        return response.json()

    def get_jobs_camxu_vip(self, cookie, uid, url="https://tuongtaccheo.com/kiemtien/camxucvipcheo/getpost.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self.httpx.get(url, params={'nickchay': uid}, headers=headers)
        return response.json()

    def get_jobs_camxu_re(self, cookie, uid, url="https://tuongtaccheo.com/kiemtien/camxucvipre/getpost.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self.httpx.get(url, params={'nickchay': uid}, headers=headers)
        return response.json()

    def get_jobs_comment(self, cookie, uid, url="https://tuongtaccheo.com/kiemtien/cmtcheo/getpost.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = self.httpx.get(url, params={'nickchay': uid}, headers=headers)
        return response.json()

    def get_coin_jobs_like_vip(self, idpost, uid, cookie, url="https://tuongtaccheo.com/kiemtien/likepostvipcheo/nhantien.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {"id": f"{idpost}", "nickchay": uid}
        response = self.httpx.post(url, headers=headers, data=data)
        return response.json()

    def get_coin_jobs_like_re(self, idpost, uid, cookie, url="https://tuongtaccheo.com/kiemtien/likepostvipre/nhantien.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {"id": f"{idpost}"}
        response = self.httpx.post(url, headers=headers, data=data)
        return response.json()

    def get_coin_jobs_camxu_vip(self, type, idpost, uid, cookie, url="https://tuongtaccheo.com/kiemtien/camxucvipcheo/nhantien.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {"id": f"{idpost}", "loaicx": type, "nickchay": uid}
        response = self.httpx.post(url, headers=headers, data=data)
        return response.json()

    def get_coin_jobs_camxu_re(self, type, idpost, uid, cookie, url="https://tuongtaccheo.com/kiemtien/camxucvipre/nhantien.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {"id": f"{idpost}", "loaicx": type, "nickchay": uid}
        response = self.httpx.post(url, headers=headers, data=data)
        return response.json()

    def get_coin_jobs_comment(self, idpost, uid, cookie, url="https://tuongtaccheo.com/kiemtien/cmtcheo/nhantien.php"):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
            "Cookie": cookie,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application",
            "X-Requested-With": "XMLHttpRequest",
        }
        data = {"id": f"{idpost}", "nickchay": uid}
        response = self.httpx.post(url, headers=headers, data=data)
        return response.json()

# ========================== PHẦN GUI (TIẾNG VIỆT) ==========================
class JobWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal()
    status_update = pyqtSignal(str, str)

    def __init__(self, accounts, ttc_cookie, ttc_uid, settings):
        super().__init__()
        self.accounts = accounts
        self.ttc_cookie = ttc_cookie
        self.ttc_uid = ttc_uid
        self.settings = settings
        self._stop = False
        self.ttc = requests_ttc()
        self.fb_action = AUTO_Action()

    def stop(self):
        self._stop = True

    def run(self):
        total_jobs = 0
        job_counter = 0
        self.log_signal.emit("[THÔNG BÁO] Bắt đầu chạy job...")

        job_types = []
        if self.settings.get('like_vip'):
            job_types.append(('like_vip', self.ttc.get_jobs_like_vip, self.ttc.get_coin_jobs_like_vip))
        if self.settings.get('like_re'):
            job_types.append(('like_re', self.ttc.get_jobs_like_re, self.ttc.get_coin_jobs_like_re))
        if self.settings.get('camxuc_vip'):
            job_types.append(('camxuc_vip', self.ttc.get_jobs_camxu_vip, self.ttc.get_coin_jobs_camxu_vip))
        if self.settings.get('camxuc_re'):
            job_types.append(('camxuc_re', self.ttc.get_jobs_camxu_re, self.ttc.get_coin_jobs_camxu_re))
        if self.settings.get('comment'):
            job_types.append(('comment', self.ttc.get_jobs_comment, self.ttc.get_coin_jobs_comment))

        if not job_types:
            self.log_signal.emit("[LỖI] Không có loại job nào được chọn.")
            self.finished_signal.emit()
            return

        for acc in self.accounts:
            if self._stop:
                break
            uid = acc['uid']
            token = acc['token']
            self.log_signal.emit(f"[THÔNG BÁO] Đang xử lý tài khoản UID: {uid}")
            self.status_update.emit(uid, "Đang chạy")

            for job_type, get_jobs_func, get_coin_func in job_types:
                if self._stop:
                    break
                self.log_signal.emit(f"[THÔNG BÁO] Đang lấy job {job_type} cho UID {uid}")
                try:
                    jobs_response = get_jobs_func(self.ttc_cookie, uid)
                except Exception as e:
                    self.log_signal.emit(f"[LỖI] Không thể lấy job {job_type}: {e}")
                    continue

                jobs = jobs_response.get('data', []) if isinstance(jobs_response, dict) else []
                if not jobs:
                    self.log_signal.emit(f"[CẢNH BÁO] Không có job {job_type} nào cho UID {uid}")
                    continue

                self.log_signal.emit(f"[THÔNG BÁO] Tìm thấy {len(jobs)} job {job_type} cho UID {uid}")
                for job in jobs:
                    if self._stop:
                        break
                    job_counter += 1
                    total_jobs += 1
                    self.progress_signal.emit(job_counter, total_jobs)

                    post_id = job.get('idpost')
                    if not post_id:
                        continue

                    try:
                        if job_type.startswith('like'):
                            reaction_type = 'LIKE'
                            self.fb_action.auto_reaction(post_id, reaction_type, token, uid)
                            self.log_signal.emit(f"[OK] Đã like bài viết {post_id}")
                        elif job_type.startswith('camxuc'):
                            emotion_type = job.get('loaicx', 'LIKE')
                            self.fb_action.auto_reaction(post_id, emotion_type, token, uid)
                            self.log_signal.emit(f"[OK] Đã cảm xúc {emotion_type} lên bài viết {post_id}")
                        elif job_type == 'comment':
                            comment_text = job.get('noidung', 'Bài viết hay!')
                            self.fb_action.auto_cmt(post_id, comment_text, token, uid)
                            self.log_signal.emit(f"[OK] Đã comment vào bài viết {post_id}")
                    except Exception as e:
                        self.log_signal.emit(f"[LỖI] Không thực hiện được hành động: {e}")
                        continue

                    try:
                        if job_type in ['like_vip', 'like_re']:
                            result = get_coin_func(post_id, uid, self.ttc_cookie)
                        elif job_type in ['camxuc_vip', 'camxuc_re']:
                            emotion_type = job.get('loaicx', 'LIKE')
                            result = get_coin_func(emotion_type, post_id, uid, self.ttc_cookie)
                        elif job_type == 'comment':
                            result = get_coin_func(post_id, uid, self.ttc_cookie)
                        self.log_signal.emit(f"[OK] Đã nhận coin cho bài viết {post_id}")
                    except Exception as e:
                        self.log_signal.emit(f"[LỖI] Không thể nhận coin: {e}")

                    if not self._stop:
                        time.sleep(self.settings.get('delay_between_jobs', 10))

                    if job_counter % self.settings.get('jobs_before_rest', 20) == 0:
                        rest = self.settings.get('rest_delay', 120)
                        self.log_signal.emit(f"[THÔNG BÁO] Nghỉ {rest} giây...")
                        time.sleep(rest)

            self.status_update.emit(uid, "Hoàn thành")

        self.log_signal.emit("[THÔNG BÁO] Job runner kết thúc.")
        self.finished_signal.emit()

class AccountCard(QGroupBox):
    run_clicked = pyqtSignal(str)
    settings_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)

    def __init__(self, account_data, parent=None):
        super().__init__(parent)
        self.account_data = account_data
        self.uid = account_data['uid']
        self.token = account_data['token']
        self.is_running = False
        self.initUI()
        self.update_info()

    def initUI(self):
        self.setTitle(f"Tài khoản {self.uid}")
        self.setStyleSheet("""
            QGroupBox {
                color: #ffffff;
                border: 2px solid #555;
                border-radius: 8px;
                margin-top: 12px;
                background-color: #3a3a3a;
                padding: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #4a6fa5;
                font-weight: bold;
            }
            QLabel { color: #ffffff; }
            QPushButton {
                background-color: #4a6fa5;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 5px 12px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #5a8ab5; }
            QPushButton:pressed { background-color: #3a5f85; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
        """)

        layout = QGridLayout()
        self.name_label = QLabel("Tên: ...")
        self.uid_label = QLabel(f"UID: {self.uid}")
        self.jobs_label = QLabel("Việc đã làm: 0")
        self.status_label = QLabel("Trạng thái: Nhàn rỗi")
        self.status_label.setStyleSheet("color: #aaa;")

        layout.addWidget(self.name_label, 0, 0)
        layout.addWidget(self.uid_label, 0, 1)
        layout.addWidget(self.jobs_label, 1, 0)
        layout.addWidget(self.status_label, 1, 1)

        self.run_btn = QPushButton("▶ Chạy")
        self.run_btn.clicked.connect(self.on_run)
        self.settings_btn = QPushButton("⚙ Cài đặt")
        self.settings_btn.clicked.connect(self.on_settings)
        self.delete_btn = QPushButton("✕ Xóa")
        self.delete_btn.clicked.connect(self.on_delete)
        self.delete_btn.setStyleSheet("background-color: #c0392b;")
        self.delete_btn.setFixedWidth(60)

        layout.addWidget(self.run_btn, 2, 0)
        layout.addWidget(self.settings_btn, 2, 1)
        layout.addWidget(self.delete_btn, 2, 2)
        self.setLayout(layout)

    def update_info(self):
        name = self.account_data.get('name', '...')
        self.name_label.setText(f"Tên: {name}")
        jobs = self.account_data.get('jobs_done', 0)
        self.jobs_label.setText(f"Việc đã làm: {jobs}")

    def set_running(self, running):
        self.is_running = running
        if running:
            self.run_btn.setText("⏹ Dừng")
            self.run_btn.setStyleSheet("background-color: #e67e22;")
            self.status_label.setText("Trạng thái: Đang chạy")
            self.status_label.setStyleSheet("color: #f1c40f;")
        else:
            self.run_btn.setText("▶ Chạy")
            self.run_btn.setStyleSheet("background-color: #4a6fa5;")
            self.status_label.setText("Trạng thái: Nhàn rỗi")
            self.status_label.setStyleSheet("color: #aaa;")

    def set_status(self, status):
        self.status_label.setText(f"Trạng thái: {status}")

    def on_run(self):
        self.run_clicked.emit(self.uid)

    def on_settings(self):
        self.settings_clicked.emit(self.uid)

    def on_delete(self):
        self.delete_clicked.emit(self.uid)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TTC Job Runner - Đăng nhập Facebook")
        self.setGeometry(100, 100, 950, 800)
        self.setStyleSheet("background-color: #2b2b2b;")

        # Khởi tạo database
        self.db = Database()
        self.load_db_settings()  # load ttc_token

        # Khởi tạo settings mặc định (sẽ bị ghi đè bởi DB)
        self.settings = {
            'like_vip': False,
            'like_re': False,
            'camxuc_vip': False,
            'camxuc_re': False,
            'comment': False,
            'delay_between_jobs': 10,
            'jobs_before_rest': 20,
            'rest_delay': 120
        }

        # Load cài đặt từ DB (ghi đè các giá trị có trong DB)
        self.load_settings_from_db()

        self.accounts = []
        self.account_widgets = {}
        self.ttc_cookie = None
        self.ttc_uid = None
        self.ttc = requests_ttc()
        self.worker = None
        self.running_uids = set()
        self._loading = True  # Cờ để bỏ qua lưu khi đang load

        # Tạo UI trước
        self.initUI()
        self.apply_styles()

        # Sau khi UI đã tạo, cập nhật các checkbox và spinbox từ self.settings
        self.load_settings_to_ui()

        # Kết nối tín hiệu SAU KHI đã load xong UI, tránh trigger lưu sai
        self.cb_like_vip.stateChanged.connect(self.on_settings_changed)
        self.cb_like_re.stateChanged.connect(self.on_settings_changed)
        self.cb_camxuc_vip.stateChanged.connect(self.on_settings_changed)
        self.cb_camxuc_re.stateChanged.connect(self.on_settings_changed)
        self.cb_comment.stateChanged.connect(self.on_settings_changed)
        self.spin_delay_jobs.valueChanged.connect(self.on_settings_changed)
        self.spin_jobs_before_rest.valueChanged.connect(self.on_settings_changed)
        self.spin_rest_delay.valueChanged.connect(self.on_settings_changed)

        self._loading = False  # Kết thúc quá trình load

        # Load tài khoản từ DB
        self.load_accounts_from_db()

        # Log trạng thái ban đầu để debug
        self.log(f"[DEBUG] Settings sau khi load: {self.settings}")

    def load_db_settings(self):
        """Load TTC token từ DB"""
        self.ttc_token = self.db.get_setting('ttc_token', '')

    def load_settings_from_db(self):
        """Đọc tất cả cài đặt từ DB và cập nhật self.settings"""
        stored = self.db.get_all_settings()
        # Cập nhật từng key nếu có trong stored
        for key in self.settings.keys():
            str_key = key  # vì key là string
            if str_key in stored:
                # Chuyển đổi giá trị từ string sang kiểu phù hợp
                value = stored[str_key]
                if isinstance(self.settings[key], bool):
                    self.settings[key] = value == 'True'
                elif isinstance(self.settings[key], int):
                    self.settings[key] = int(value)
                # Nếu là kiểu khác thì giữ nguyên
        # Log để debug
        print(f"[DEBUG] Loaded settings from DB: {self.settings}")

    def save_settings_to_db(self):
        """Lưu toàn bộ self.settings vào DB"""
        if self._loading:
            return  # Không lưu trong khi đang load
        for key, value in self.settings.items():
            # Chuyển đổi giá trị sang string
            self.db.set_setting(key, str(value))
        print(f"[DEBUG] Saved settings to DB: {self.settings}")

    def load_accounts_from_db(self):
        """Load tài khoản từ DB và hiển thị lên UI"""
        db_accounts = self.db.get_all_accounts()
        for acc in db_accounts:
            # Giải mã mật khẩu và secret nếu cần (không lưu vào memory, chỉ dùng token)
            uid = acc['uid']
            token = acc['token']
            name = acc['name'] if acc['name'] else uid
            jobs_done = acc['jobs_done']

            if not token:
                continue  # bỏ qua account không có token

            acc_data = {
                'uid': uid,
                'token': token,
                'name': name,
                'jobs_done': jobs_done,
                'email': acc['email']  # lưu thêm để tham khảo
            }
            self.accounts.append(acc_data)
            self.add_account_card(acc_data)

    def initUI(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        # ----- Tab Tài khoản -----
        accounts_tab = QWidget()
        accounts_layout = QVBoxLayout(accounts_tab)

        add_layout = QHBoxLayout()
        self.add_account_btn = QPushButton("➕ Thêm tài khoản Facebook")
        self.add_account_btn.clicked.connect(self.show_add_account_dialog)
        add_layout.addWidget(self.add_account_btn)
        add_layout.addStretch()
        accounts_layout.addLayout(add_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: #2b2b2b; }")
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setAlignment(Qt.AlignTop)
        self.cards_layout.setSpacing(10)
        scroll.setWidget(self.cards_container)
        accounts_layout.addWidget(scroll)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        accounts_layout.addWidget(self.progress)

        control_layout = QHBoxLayout()
        self.start_all_btn = QPushButton("▶ Chạy tất cả")
        self.start_all_btn.clicked.connect(self.start_all)
        self.stop_all_btn = QPushButton("⏹ Dừng tất cả")
        self.stop_all_btn.clicked.connect(self.stop_all)
        self.stop_all_btn.setEnabled(False)
        control_layout.addWidget(self.start_all_btn)
        control_layout.addWidget(self.stop_all_btn)
        control_layout.addStretch()
        accounts_layout.addLayout(control_layout)

        accounts_tab.setLayout(accounts_layout)
        self.tab_widget.addTab(accounts_tab, "Tài khoản")

        # ----- Tab Logs -----
        logs_tab = QWidget()
        logs_layout = QVBoxLayout(logs_tab)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setStyleSheet("background-color: #1e1e1e; color: #ffffff;")
        logs_layout.addWidget(self.log_text)
        clear_log_btn = QPushButton("Xóa log")
        clear_log_btn.clicked.connect(lambda: self.log_text.clear())
        logs_layout.addWidget(clear_log_btn)
        logs_tab.setLayout(logs_layout)
        self.tab_widget.addTab(logs_tab, "Nhật ký")

        # ----- Tab Cài đặt -----
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)

        ttc_group = QGroupBox("Token truy cập TTC")
        ttc_group.setStyleSheet("QGroupBox { color: #ffffff; border: 1px solid #555; border-radius: 5px; margin-top: 10px; }")
        ttc_layout = QHBoxLayout()
        self.ttc_token_edit = QLineEdit()
        self.ttc_token_edit.setPlaceholderText("Nhập token truy cập TTC")
        self.ttc_token_edit.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        if hasattr(self, 'ttc_token'):
            self.ttc_token_edit.setText(self.ttc_token)
        ttc_layout.addWidget(self.ttc_token_edit)
        self.ttc_login_btn = QPushButton("Đăng nhập")
        self.ttc_login_btn.clicked.connect(self.login_ttc)
        ttc_layout.addWidget(self.ttc_login_btn)
        ttc_group.setLayout(ttc_layout)
        settings_layout.addWidget(ttc_group)

        job_group = QGroupBox("Loại công việc")
        job_group.setStyleSheet("QGroupBox { color: #ffffff; border: 1px solid #555; border-radius: 5px; margin-top: 10px; }")
        job_layout = QVBoxLayout()
        self.cb_like_vip = QCheckBox("Like VIP")
        self.cb_like_re = QCheckBox("Like RE")
        self.cb_camxuc_vip = QCheckBox("Cảm xúc VIP")
        self.cb_camxuc_re = QCheckBox("Cảm xúc RE")
        self.cb_comment = QCheckBox("Comment")
        # Không connect tín hiệu ở đây, sẽ connect sau khi load
        for cb in [self.cb_like_vip, self.cb_like_re, self.cb_camxuc_vip,
                   self.cb_camxuc_re, self.cb_comment]:
            cb.setStyleSheet("color: #ffffff; background-color: #3a3a3a; padding: 5px; border-radius: 3px;")
            job_layout.addWidget(cb)
        job_group.setLayout(job_layout)
        settings_layout.addWidget(job_group)

        delay_group = QGroupBox("Cài đặt độ trễ")
        delay_group.setStyleSheet("QGroupBox { color: #ffffff; border: 1px solid #555; border-radius: 5px; margin-top: 10px; }")
        delay_layout = QVBoxLayout()
        h1 = QHBoxLayout()
        h1.addWidget(QLabel("Khoảng cách giữa các job (giây):"))
        self.spin_delay_jobs = QSpinBox()
        self.spin_delay_jobs.setMinimum(10)
        self.spin_delay_jobs.setValue(10)
        self.spin_delay_jobs.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        h1.addWidget(self.spin_delay_jobs)
        delay_layout.addLayout(h1)

        h2 = QHBoxLayout()
        h2.addWidget(QLabel("Số job trước khi nghỉ:"))
        self.spin_jobs_before_rest = QSpinBox()
        self.spin_jobs_before_rest.setMinimum(20)
        self.spin_jobs_before_rest.setValue(20)
        self.spin_jobs_before_rest.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        h2.addWidget(self.spin_jobs_before_rest)
        delay_layout.addLayout(h2)

        h3 = QHBoxLayout()
        h3.addWidget(QLabel("Thời gian nghỉ (giây):"))
        self.spin_rest_delay = QSpinBox()
        self.spin_rest_delay.setMinimum(120)
        self.spin_rest_delay.setValue(120)
        self.spin_rest_delay.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        h3.addWidget(self.spin_rest_delay)
        delay_layout.addLayout(h3)

        delay_group.setLayout(delay_layout)
        settings_layout.addWidget(delay_group)
        settings_layout.addStretch()

        settings_tab.setLayout(settings_layout)
        self.tab_widget.addTab(settings_tab, "Cài đặt")

    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #2b2b2b; }
            QTabWidget::pane { border: 1px solid #555; background-color: #2b2b2b; }
            QTabBar::tab {
                background-color: #3c3c3c; color: #ddd; padding: 6px 12px;
                margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px;
            }
            QTabBar::tab:selected { background-color: #4a6fa5; color: white; }
            QTabBar::tab:hover { background-color: #5a8ab5; }
            QLabel { color: #ffffff; }
            QLineEdit, QTextEdit, QSpinBox {
                background-color: #3c3c3c; color: #ffffff; border: 1px solid #555; border-radius: 3px; padding: 5px;
            }
            QPushButton {
                background-color: #4a6fa5; color: white; border: none; border-radius: 4px; padding: 6px 12px; font-weight: bold;
            }
            QPushButton:hover { background-color: #5a8ab5; }
            QPushButton:pressed { background-color: #3a5f85; }
            QPushButton:disabled { background-color: #555; color: #aaa; }
            QProgressBar {
                background-color: #3c3c3c; color: white; border: 1px solid #555; border-radius: 4px; text-align: center;
            }
            QProgressBar::chunk { background-color: #4a6fa5; border-radius: 4px; }
            QCheckBox {
                color: #ffffff;
                background-color: #3a3a3a;
                padding: 5px;
                border-radius: 3px;
            }
            QMessageBox {
                background-color: #2b2b2b;
                color: #ffffff;
            }
            QMessageBox QLabel {
                color: #ffffff;
            }
            QMessageBox QPushButton {
                background-color: #4a6fa5;
                color: white;
                min-width: 80px;
                padding: 5px;
            }
            QMessageBox QPushButton:hover {
                background-color: #5a8ab5;
            }
        """)

    def show_add_account_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Thêm tài khoản Facebook")
        dialog.setModal(True)
        dialog.setFixedSize(500, 250)
        dialog.setStyleSheet("QDialog { background-color: #2b2b2b; }")

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Email / Số điện thoại:"))
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("Nhập email/số điện thoại đăng nhập Facebook")
        self.email_edit.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        layout.addWidget(self.email_edit)

        layout.addWidget(QLabel("Mật khẩu:"))
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setPlaceholderText("Nhập mật khẩu Facebook")
        self.pwd_edit.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        layout.addWidget(self.pwd_edit)

        layout.addWidget(QLabel("Mã bí mật 2FA (tùy chọn):"))
        self.secret_edit = QLineEdit()
        self.secret_edit.setPlaceholderText("Để trống nếu không có 2FA")
        self.secret_edit.setStyleSheet("background-color: #3c3c3c; color: #ffffff; border: 1px solid #555;")
        layout.addWidget(self.secret_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.add_account_from_login)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)
        dialog.exec_()

    def add_account_from_login(self):
        email = self.email_edit.text().strip()
        password = self.pwd_edit.text().strip()
        secret = self.secret_edit.text().strip()

        if not email or not password:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập email và mật khẩu.")
            return

        self.log(f"[THÔNG BÁO] Đang đăng nhập Facebook cho {email}...")
        try:
            fb_login = Requests_facebook()
            token = fb_login.login_and_get_token(email, password, secret)
            if not token:
                QMessageBox.critical(self, "Lỗi", "Đăng nhập thất bại. Kiểm tra thông tin hoặc 2FA.")
                return

            fb_action = AUTO_Action()
            uid, msg = fb_action.get_info(token)
            if not uid:
                QMessageBox.critical(self, "Lỗi", f"Token không hợp lệ: {msg}")
                return

            name = uid
            if any(acc['uid'] == uid for acc in self.accounts):
                QMessageBox.warning(self, "Lỗi", "Tài khoản đã tồn tại.")
                return

            # Mã hóa và lưu vào DB
            password_enc = encrypt_text(password)
            secret_enc = encrypt_text(secret)
            self.db.add_account(email, password_enc, secret_enc, token, uid, name)

            acc_data = {
                'uid': uid,
                'token': token,
                'name': name,
                'jobs_done': 0,
                'email': email
            }
            self.accounts.append(acc_data)
            self.add_account_card(acc_data)
            self.log(f"[OK] Đã thêm tài khoản Facebook UID: {uid}")
            QMessageBox.information(self, "Thành công", f"Đã thêm tài khoản UID: {uid}")

        except Exception as e:
            self.log(f"[LỖI] Lỗi đăng nhập: {e}")
            QMessageBox.critical(self, "Lỗi", f"Lỗi: {e}")

    def add_account_card(self, acc_data):
        card = AccountCard(acc_data)
        card.run_clicked.connect(self.on_run_account)
        card.settings_clicked.connect(self.on_settings_account)
        card.delete_clicked.connect(self.on_delete_account)
        self.cards_layout.addWidget(card)
        self.account_widgets[acc_data['uid']] = card

    def on_run_account(self, uid):
        if uid in self.running_uids:
            self.stop_account(uid)
        else:
            self.start_account(uid)

    def start_account(self, uid):
        acc = next((a for a in self.accounts if a['uid'] == uid), None)
        if not acc:
            return
        if not self.ttc_cookie:
            QMessageBox.warning(self, "Lỗi", "Vui lòng đăng nhập TTC trước.")
            return
        if not any([self.settings.get('like_vip'), self.settings.get('like_re'),
                    self.settings.get('camxuc_vip'), self.settings.get('camxuc_re'),
                    self.settings.get('comment')]):
            QMessageBox.warning(self, "Lỗi", "Chưa chọn loại job trong cài đặt.")
            return

        card = self.account_widgets.get(uid)
        if card:
            card.set_running(True)
        self.running_uids.add(uid)

        if self.worker is None or not self.worker.isRunning():
            selected = [a for a in self.accounts if a['uid'] in self.running_uids]
            self.worker = JobWorker(selected, self.ttc_cookie, self.ttc_uid, self.settings)
            self.worker.log_signal.connect(self.log)
            self.worker.progress_signal.connect(self.update_progress)
            self.worker.finished_signal.connect(self.on_worker_finished)
            self.worker.status_update.connect(self.update_account_status)
            self.worker.start()
            self.start_all_btn.setEnabled(False)
            self.stop_all_btn.setEnabled(True)

    def stop_account(self, uid):
        if uid in self.running_uids:
            self.running_uids.remove(uid)
            card = self.account_widgets.get(uid)
            if card:
                card.set_running(False)
            if not self.running_uids and self.worker and self.worker.isRunning():
                self.worker.stop()
                self.worker = None
                self.start_all_btn.setEnabled(True)
                self.stop_all_btn.setEnabled(False)

    def on_settings_account(self, uid):
        acc = next((a for a in self.accounts if a['uid'] == uid), None)
        if not acc:
            return
        QMessageBox.information(self, "Thông tin tài khoản", f"UID: {acc['uid']}\nToken: {acc['token'][:20]}...")

    def on_delete_account(self, uid):
        reply = QMessageBox.question(self, 'Xác nhận', f'Xóa tài khoản {uid}?',
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.stop_account(uid)
            self.accounts = [a for a in self.accounts if a['uid'] != uid]
            self.db.delete_account(uid)
            card = self.account_widgets.pop(uid, None)
            if card:
                self.cards_layout.removeWidget(card)
                card.deleteLater()

    def start_all(self):
        if not self.ttc_cookie:
            QMessageBox.warning(self, "Lỗi", "Vui lòng đăng nhập TTC trước.")
            return
        if not any([self.settings.get('like_vip'), self.settings.get('like_re'),
                    self.settings.get('camxuc_vip'), self.settings.get('camxuc_re'),
                    self.settings.get('comment')]):
            QMessageBox.warning(self, "Lỗi", "Chưa chọn loại job trong cài đặt.")
            return

        selected = [a for a in self.accounts if a['uid']]
        if not selected:
            QMessageBox.warning(self, "Lỗi", "Không có tài khoản hợp lệ.")
            return

        for acc in selected:
            self.running_uids.add(acc['uid'])
            card = self.account_widgets.get(acc['uid'])
            if card:
                card.set_running(True)

        self.worker = JobWorker(selected, self.ttc_cookie, self.ttc_uid, self.settings)
        self.worker.log_signal.connect(self.log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_worker_finished)
        self.worker.status_update.connect(self.update_account_status)
        self.worker.start()
        self.start_all_btn.setEnabled(False)
        self.stop_all_btn.setEnabled(True)

    def stop_all(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker = None
        for uid in list(self.running_uids):
            self.stop_account(uid)
        self.start_all_btn.setEnabled(True)
        self.stop_all_btn.setEnabled(False)

    def on_worker_finished(self):
        self.start_all_btn.setEnabled(True)
        self.stop_all_btn.setEnabled(False)
        self.progress.setValue(0)
        self.log("[THÔNG BÁO] Worker kết thúc.")
        for uid in list(self.running_uids):
            self.stop_account(uid)
        self.running_uids.clear()

    def update_account_status(self, uid, status):
        card = self.account_widgets.get(uid)
        if card:
            card.set_status(status)
            if status == "Hoàn thành":
                card.set_running(False)
                if uid in self.running_uids:
                    self.running_uids.remove(uid)

    def update_progress(self, current, total):
        self.progress.setMaximum(total if total > 0 else 1)
        self.progress.setValue(current)

    def login_ttc(self):
        token = self.ttc_token_edit.text().strip()
        if not token:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập token TTC.")
            return
        self.log("[THÔNG BÁO] Đang đăng nhập TTC...")
        try:
            data, cookie = self.ttc.get_accout(token)
            if cookie:
                self.ttc_cookie = cookie
                self.ttc_uid = data.get('uid') if isinstance(data, dict) else None
                self.db.set_setting('ttc_token', token)
                self.log(f"[THÔNG BÁO] Đăng nhập TTC thành công. UID: {self.ttc_uid}")
                QMessageBox.information(self, "Thành công", "Đăng nhập TTC thành công.")
            else:
                self.log(f"[LỖI] Đăng nhập TTC thất bại: {data}")
                QMessageBox.critical(self, "Lỗi", f"Đăng nhập thất bại: {data}")
        except Exception as e:
            self.log(f"[LỖI] Lỗi đăng nhập TTC: {e}")
            QMessageBox.critical(self, "Lỗi", f"Lỗi: {e}")

    def load_settings_to_ui(self):
        """Cập nhật giao diện từ self.settings"""
        self.cb_like_vip.setChecked(self.settings.get('like_vip', False))
        self.cb_like_re.setChecked(self.settings.get('like_re', False))
        self.cb_camxuc_vip.setChecked(self.settings.get('camxuc_vip', False))
        self.cb_camxuc_re.setChecked(self.settings.get('camxuc_re', False))
        self.cb_comment.setChecked(self.settings.get('comment', False))
        self.spin_delay_jobs.setValue(self.settings.get('delay_between_jobs', 10))
        self.spin_jobs_before_rest.setValue(self.settings.get('jobs_before_rest', 20))
        self.spin_rest_delay.setValue(self.settings.get('rest_delay', 120))
        print(f"[DEBUG] UI updated with settings: {self.settings}")

    def on_settings_changed(self):
        """Xử lý khi bất kỳ control settings thay đổi"""
        # Nếu đang trong quá trình load thì bỏ qua
        if self._loading:
            return
        # Cập nhật self.settings từ UI
        self.settings['like_vip'] = self.cb_like_vip.isChecked()
        self.settings['like_re'] = self.cb_like_re.isChecked()
        self.settings['camxuc_vip'] = self.cb_camxuc_vip.isChecked()
        self.settings['camxuc_re'] = self.cb_camxuc_re.isChecked()
        self.settings['comment'] = self.cb_comment.isChecked()
        self.settings['delay_between_jobs'] = self.spin_delay_jobs.value()
        self.settings['jobs_before_rest'] = self.spin_jobs_before_rest.value()
        self.settings['rest_delay'] = self.spin_rest_delay.value()
        # Lưu ngay vào DB
        self.save_settings_to_db()
        print(f"[DEBUG] Settings changed: {self.settings}")

    def log(self, msg):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.append(f"[{timestamp}] {msg}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(self, 'Xác nhận', 'Job đang chạy. Bạn có muốn thoát?',
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.worker.stop()
                self.worker.wait()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())