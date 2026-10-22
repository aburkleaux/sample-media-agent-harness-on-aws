import IconBase, { type IconProps } from './IconBase';

/** Image asset: a frame with a sun and a hill. */
export default function ImageIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <circle cx="9" cy="9.5" r="1.5" />
      <path d="M21 16l-5-5-8 9" />
    </IconBase>
  );
}
