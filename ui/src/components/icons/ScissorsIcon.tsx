import IconBase, { type IconProps } from './IconBase';

/** Clip / trim. */
export default function ScissorsIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <circle cx="6" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M8.1 8.1L20 20M8.1 15.9L20 4" />
    </IconBase>
  );
}
