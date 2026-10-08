/** Multimodal message body (corresponds to Python type/__init__.py MultiModalMessage) */
export interface MultiModalMessage {
  /** Text content */
  text: string;
  /** Image path list */
  image_path_list?: string[];
  /** Image byte data (base64 string) */
  image_bytes_list?: string[];
  /** Image base64 list */
  image_base64_list?: string[];
  /** Audio path list */
  audio_path_list?: string[];
  /** Audio byte data (base64 string) */
  audio_bytes_list?: string[];
  /** Video path list */
  video_path_list?: string[];
  /** Video byte data (base64 string) */
  video_bytes_list?: string[];
}
