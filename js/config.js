/**
 * config.js — 前端共享配置常量
 *
 * 统一本地增强服务地址，供各 Tab / 组件 import。
 * 原先每个文件各自声明服务地址（6 处重复），且需 build_standalone.py
 * 的 COLLIDING 机制在内联时逐个改名防冲突；集中到此处后：
 *   - HTTP 模式：各文件 import { SERVER } 正常工作
 *   - standalone 模式：build 脚本删除 import，仅本文件保留唯一声明
 *     （本文件在 ORDER_WITH_PREFIX 中排最前，无需 COLLIDING 改名）
 */
export const SERVER = 'http://127.0.0.1:7788';
