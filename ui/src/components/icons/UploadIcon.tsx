import IconBase, { type IconProps } from './IconBase';

/** Upload: an arrow rising out of a tray. */
export default function UploadIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M12 15V4" />
      <path d="M7.5 8.5L12 4l4.5 4.5" />
      <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
    </IconBase>
  );
}
