import IconBase, { type IconProps } from './IconBase';

/** Sound on: a speaker with two waves. */
export default function VolumeIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M11 5L6 9H3v6h3l5 4V5z" />
      <path d="M15.5 8.5a5 5 0 010 7M18.5 5.5a9 9 0 010 13" />
    </IconBase>
  );
}
