import IconBase, { type IconProps } from './IconBase';

/** An S3 location: a cloud. */
export default function CloudIcon(props: IconProps) {
  return (
    <IconBase {...props}>
      <path d="M7 18a4.5 4.5 0 0 1-.5-8.97A6 6 0 0 1 18 10a4 4 0 0 1 0 8H7z" />
    </IconBase>
  );
}
