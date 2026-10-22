import IconBase, { type IconProps } from './IconBase';

/** Video asset: a frame with a play triangle. */
export default function VideoIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M10 9.5v5l4.5-2.5L10 9.5z" />
    </IconBase>
  );
}
