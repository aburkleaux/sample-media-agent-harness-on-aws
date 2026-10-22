import IconBase, { type IconProps } from './IconBase';

/** Sound off: a speaker with a cross. */
export default function MuteIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M11 5L6 9H3v6h3l5 4V5z" />
      <path d="M16 9l5 6M21 9l-5 6" />
    </IconBase>
  );
}
