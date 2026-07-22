# -*- coding: UTF-8 -*-

import json
import os
import copy

from modules import ui_extra_networks
from scripts.model_metadata_service import read_model_display_info

filters = [
    # 'filename',
    # 'description',
    'search_term',
    'local_preview',
    'metadata',
]


def get_extra_networks():
    result = []
    try:
        for extra_page in list(ui_extra_networks.extra_pages):
            result_item = {
                'name': extra_page.name,
                'title': extra_page.title,
                'items': []
            }
            for oriItem in extra_page.list_items():
                item = copy.deepcopy(oriItem)
                # 解析metadata
                output_name = None
                try:
                    if 'metadata' in item and item['metadata']:
                        metadata = json.loads(item['metadata'])
                        if metadata and 'ss_output_name' in metadata:
                            output_name = metadata['ss_output_name']
                except Exception as e:
                    pass
                item['output_name'] = output_name

                # Read optional Browser Neo artifacts and independent
                # .civitai.info through the shared, read-only metadata backend.
                item['civitai_info'] = {}
                try:
                    if 'filename' in item and item['filename']:
                        item['basename'] = os.path.basename(item['filename'])
                        item['dirname'] = os.path.dirname(item['filename'])
                        info = read_model_display_info(item['filename'])
                        if info.get('sources'):
                            item['civitai_info'] = info
                except Exception:
                    pass

                # 过滤掉不需要的字段
                newItem = {}
                for key in item:
                    if key not in filters:
                        newItem[key] = item[key]

                result_item['items'].append(newItem)

            result.append(result_item)
    except Exception as e:
        print(f'[sd-webui-prompt-all-in-one] get_extra_networks error: {e}')
        pass
    return result
